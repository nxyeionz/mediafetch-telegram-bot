from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import math
import os
from pathlib import Path
from typing import BinaryIO, Optional, Tuple

from telethon import TelegramClient, helpers, utils
from telethon.crypto import AuthKey
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import (
    ExportAuthorizationRequest,
    ImportAuthorizationRequest,
)
from telethon.tl.functions.upload import (
    SaveBigFilePartRequest,
    SaveFilePartRequest,
)
from telethon.tl.types import (
    InputFile,
    InputFileBig,
    TypeInputFile,
)

logger = logging.getLogger(__name__)


class UploadSender:
    def __init__(
        self,
        client: TelegramClient,
        sender: MTProtoSender,
        file_id: int,
        part_count: int,
        big: bool,
        index: int,
        stride: int,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.client = client
        self.sender = sender
        self.part_count = part_count
        if big:
            self.request = SaveBigFilePartRequest(file_id, index, part_count, b"")
        else:
            self.request = SaveFilePartRequest(file_id, index, b"")
        self.stride = stride
        self.previous: Optional[asyncio.Task] = None
        self.loop = loop

    async def next(self, data: bytes) -> None:
        if self.previous:
            await self.previous
        self.previous = self.loop.create_task(self._next(data))

    async def _next(self, data: bytes) -> None:
        self.request.bytes = data
        await self.client._call(self.sender, self.request)
        self.request.file_part += self.stride

    async def disconnect(self) -> None:
        if self.previous:
            await self.previous
        return await self.sender.disconnect()


class ParallelUploader:
    def __init__(self, client: TelegramClient, dc_id: Optional[int] = None) -> None:
        self.client = client
        self.loop = self.client.loop
        self.dc_id = dc_id or self.client.session.dc_id
        self.auth_key = (
            None
            if dc_id and self.client.session.dc_id != dc_id
            else self.client.session.auth_key
        )
        self.senders: list[UploadSender] = []
        self.upload_ticker = 0

    async def _cleanup(self) -> None:
        if self.senders:
            await asyncio.gather(*[sender.disconnect() for sender in self.senders], return_exceptions=True)
            self.senders = []

    @staticmethod
    def _get_connection_count(file_size: int, max_count: int = 12) -> int:
        if file_size > 50 * 1024 * 1024:
            return max_count
        if file_size > 10 * 1024 * 1024:
            return min(8, max_count)
        return min(4, max_count)

    async def _create_sender(self) -> MTProtoSender:
        dc = await self.client._get_dc(self.dc_id)
        sender = MTProtoSender(self.auth_key, loggers=self.client._log)
        await sender.connect(
            self.client._connection(
                dc.ip_address,
                dc.port,
                dc.id,
                loggers=self.client._log,
                proxy=self.client._proxy,
            )
        )
        if not self.auth_key:
            auth = await self.client(ExportAuthorizationRequest(self.dc_id))
            self.client._init_request.query = ImportAuthorizationRequest(
                id=auth.id, bytes=auth.bytes
            )
            req = InvokeWithLayerRequest(LAYER, self.client._init_request)
            await sender.send(req)
            self.auth_key = sender.auth_key
        return sender

    async def _create_upload_sender(
        self, file_id: int, part_count: int, big: bool, index: int, stride: int
    ) -> UploadSender:
        sender = await self._create_sender()
        return UploadSender(
            self.client,
            sender,
            file_id,
            part_count,
            big,
            index,
            stride,
            loop=self.loop,
        )

    async def init_upload(
        self,
        file_id: int,
        file_size: int,
        part_size_kb: Optional[float] = 512,
        connection_count: Optional[int] = None,
    ) -> Tuple[int, int, bool]:
        connection_count = connection_count or self._get_connection_count(file_size)
        part_size = int((part_size_kb or utils.get_appropriated_part_size(file_size)) * 1024)
        part_count = (file_size + part_size - 1) // part_size
        is_large = file_size > 10 * 1024 * 1024

        self.senders = [
            await self._create_upload_sender(file_id, part_count, is_large, 0, connection_count),
            *await asyncio.gather(
                *[
                    self._create_upload_sender(file_id, part_count, is_large, i, connection_count)
                    for i in range(1, connection_count)
                ]
            ),
        ]
        return part_size, part_count, is_large

    async def upload(self, part: bytes) -> None:
        await self.senders[self.upload_ticker].next(part)
        self.upload_ticker = (self.upload_ticker + 1) % len(self.senders)

    async def finish_upload(self) -> None:
        await self._cleanup()


async def fast_upload_file(
    client: TelegramClient,
    file_path: Path | str,
    name: Optional[str] = None,
    progress_callback: Optional[callable] = None,
    connection_count: int = 12,
    part_size_kb: int = 512,
) -> TypeInputFile:
    path = Path(file_path)
    file_name = name or path.name
    file_size = path.stat().st_size
    file_id = helpers.generate_random_long()

    uploader = ParallelUploader(client)
    try:
        part_size, part_count, is_large = await uploader.init_upload(
            file_id,
            file_size,
            part_size_kb=part_size_kb,
            connection_count=connection_count,
        )

        hash_md5 = hashlib.md5()
        buffer = bytearray()
        bytes_read = 0

        with open(path, "rb") as f:
            chunk_read_size = 128 * 1024
            while True:
                data = f.read(chunk_read_size)
                if not data:
                    break
                bytes_read += len(data)
                if not is_large:
                    hash_md5.update(data)
                if progress_callback:
                    r = progress_callback(bytes_read, file_size)
                    if inspect.isawaitable(r):
                        try:
                            await r
                        except BaseException:
                            pass

                if len(buffer) == 0 and len(data) == part_size:
                    await uploader.upload(data)
                    continue

                buffer.extend(data)
                while len(buffer) >= part_size:
                    chunk = bytes(buffer[:part_size])
                    del buffer[:part_size]
                    await uploader.upload(chunk)

            if len(buffer) > 0:
                await uploader.upload(bytes(buffer))

        await uploader.finish_upload()

        if is_large:
            return InputFileBig(file_id, part_count, file_name)
        else:
            return InputFile(file_id, part_count, file_name, hash_md5.hexdigest())
    except Exception:
        await uploader._cleanup()
        raise
