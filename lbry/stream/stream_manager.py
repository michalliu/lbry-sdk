import os
import asyncio
import binascii
import logging
import random
import typing
from typing import Optional
from aiohttp.web import Request
from lbry.error import InvalidStreamDescriptorError
from lbry.file.source_manager import SourceManager
from lbry.stream.descriptor import StreamDescriptor
from lbry.stream.managed_stream import ManagedStream
from lbry.file.source import ManagedDownloadSource
if typing.TYPE_CHECKING:
    from lbry.conf import Config
    from lbry.blob.blob_manager import BlobManager
    from lbry.dht.node import Node
    from lbry.wallet.wallet import WalletManager
    from lbry.wallet.transaction import Transaction
    from lbry.extras.daemon.analytics import AnalyticsManager
    from lbry.extras.daemon.storage import SQLiteStorage, StoredContentClaim

log = logging.getLogger(__name__)


def path_or_none(p) -> Optional[str]:
    if not p:
        return
    return binascii.unhexlify(p).decode()


class StreamManager(SourceManager):
    _sources: typing.Dict[str, ManagedStream]

    filter_fields = set(SourceManager.filter_fields)
    filter_fields.update({
        'sd_hash',
        'stream_hash',
        'full_status',  # TODO: remove
        'blobs_remaining',
        'blobs_in_stream'
    })

    def __init__(self, loop: asyncio.AbstractEventLoop, config: 'Config', blob_manager: 'BlobManager',
                 wallet_manager: 'WalletManager', storage: 'SQLiteStorage', node: Optional['Node'],
                 analytics_manager: Optional['AnalyticsManager'] = None):
        super().__init__(loop, config, storage, analytics_manager)
        self.blob_manager = blob_manager
        self.wallet_manager = wallet_manager
        self.node = node
        self.resume_saving_task: Optional[asyncio.Task] = None
        self.re_reflect_task: Optional[asyncio.Task] = None
        self.update_stream_finished_futs: typing.List[asyncio.Future] = []
        self.running_reflector_uploads: typing.Dict[str, asyncio.Task] = {}
        self.started = asyncio.Event(loop=self.loop)

    def add(self, source: ManagedStream):
        super().add(source)
        self.storage.content_claim_callbacks[source.stream_hash] = lambda: self._update_content_claim(source)

    async def _update_content_claim(self, stream: ManagedStream):
        claim_info = await self.storage.get_content_claim(stream.stream_hash)
        self._sources.setdefault(stream.sd_hash, stream).set_claim(claim_info, claim_info['value'])

    async def recover_streams(self, file_infos: typing.List[typing.Dict]):
        to_restore = []

        async def recover_stream(sd_hash: str, stream_hash: str, stream_name: str,
                                 suggested_file_name: str, key: str,
                                 content_fee: Optional['Transaction']) -> Optional[StreamDescriptor]:
            sd_blob = self.blob_manager.get_blob(sd_hash)
            blobs = await self.storage.get_blobs_for_stream(stream_hash)
            descriptor = await StreamDescriptor.recover(
                self.blob_manager.blob_dir, sd_blob, stream_hash, stream_name, suggested_file_name, key, blobs
            )
            if not descriptor:
                return
            to_restore.append((descriptor, sd_blob, content_fee))

        await asyncio.gather(*[
            recover_stream(
                file_info['sd_hash'], file_info['stream_hash'], binascii.unhexlify(file_info['stream_name']).decode(),
                binascii.unhexlify(file_info['suggested_file_name']).decode(), file_info['key'],
                file_info['content_fee']
            ) for file_info in file_infos
        ])

        if to_restore:
            await self.storage.recover_streams(to_restore, self.config.download_dir)

        # if self.blob_manager._save_blobs:
        #     log.info("Recovered %i/%i attempted streams", len(to_restore), len(file_infos))

    async def _load_stream(self, rowid: int, sd_hash: str, file_name: Optional[str],
                           download_directory: Optional[str], status: str,
                           claim: Optional['StoredContentClaim'], content_fee: Optional['Transaction'],
                           added_on: Optional[int]):
        try:
            descriptor = await self.blob_manager.get_stream_descriptor(sd_hash)
        except InvalidStreamDescriptorError as err:
            log.warning("Failed to start stream for sd %s - %s", sd_hash, str(err))
            return
        stream = ManagedStream(
            self.loop, self.config, self.blob_manager, descriptor.sd_hash, download_directory, file_name, status,
            claim, content_fee=content_fee, rowid=rowid, descriptor=descriptor,
            analytics_manager=self.analytics_manager, added_on=added_on
        )
        if fully_reflected:
            stream.fully_reflected.set()
        self.add(stream)

    async def initialize_from_database(self):
        to_recover = []
        to_start = []

        await self.storage.update_manually_removed_files_since_last_run()

        for file_info in await self.storage.get_all_lbry_files():
            # if the sd blob is not verified, try to reconstruct it from the database
            # this could either be because the blob files were deleted manually or save_blobs was not true when
            # the stream was downloaded
            if not self.blob_manager.is_blob_verified(file_info['sd_hash']):
                to_recover.append(file_info)
            to_start.append(file_info)
        if to_recover:
            await self.recover_streams(to_recover)

        log.info("Initializing %i files", len(to_start))
        to_resume_saving = []
        add_stream_tasks = []
        for file_info in to_start:
            file_name = path_or_none(file_info['file_name'])
            download_directory = path_or_none(file_info['download_directory'])
            if file_name and download_directory and not file_info['saved_file'] and file_info['status'] == 'running':
                to_resume_saving.append((file_name, download_directory, file_info['sd_hash']))
            add_stream_tasks.append(self.loop.create_task(self._load_stream(
                file_info['rowid'], file_info['sd_hash'], file_name,
                download_directory, file_info['status'],
                file_info['claim'], file_info['content_fee'],
                file_info['added_on'], file_info['fully_reflected']
            )))
        if add_stream_tasks:
            await asyncio.gather(*add_stream_tasks, loop=self.loop)
        log.info("Started stream manager with %i files", len(self._sources))
        if not self.node:
            log.info("no DHT node given, resuming downloads trusting that we can contact reflector")
        if to_resume_saving:
            log.info("Resuming saving %i files", len(to_resume_saving))
            self.resume_saving_task = self.loop.create_task(asyncio.gather(
                *(self._sources[sd_hash].save_file(file_name, download_directory, node=self.node)
                  for (file_name, download_directory, sd_hash) in to_resume_saving),
                loop=self.loop
            ))

    async def reflect_streams(self):
        while True:
            if self.config.reflect_streams and self.config.reflector_servers:
                sd_hashes = await self.storage.get_streams_to_re_reflect()
                sd_hashes = [sd for sd in sd_hashes if sd in self._sources]
                batch = []
                while sd_hashes:
                    stream = self.streams[sd_hashes.pop()]
                    if self.blob_manager.is_blob_verified(stream.sd_hash) and stream.blobs_completed and \
                            stream.sd_hash not in self.running_reflector_uploads and not \
                            stream.fully_reflected.is_set():
                        batch.append(self.reflect_stream(stream))
                    if len(batch) >= self.config.concurrent_reflector_uploads:
                        await asyncio.gather(*batch, loop=self.loop)
                        batch = []
                if batch:
                    await asyncio.gather(*batch, loop=self.loop)
            await asyncio.sleep(300, loop=self.loop)

    async def start(self):
        await super().start()
        self.re_reflect_task = self.loop.create_task(self.reflect_streams())

    def stop(self):
        if self.resume_saving_task and not self.resume_saving_task.done():
            self.resume_saving_task.cancel()
        if self.re_reflect_task and not self.re_reflect_task.done():
            self.re_reflect_task.cancel()
        while self.update_stream_finished_futs:
            self.update_stream_finished_futs.pop().cancel()
        while self.running_reflector_uploads:
            _, t = self.running_reflector_uploads.popitem()
            t.cancel()
        self.started.clear()
        log.info("finished stopping the stream manager")

    def reflect_stream(self, stream: ManagedStream, server: Optional[str] = None,
                       port: Optional[int] = None) -> asyncio.Task:
        if not server or not port:
            server, port = random.choice(self.config.reflector_servers)
        if stream.sd_hash in self.running_reflector_uploads:
            return self.running_reflector_uploads[stream.sd_hash]
        task = self.loop.create_task(stream.upload_to_reflector(server, port))
        self.running_reflector_uploads[stream.sd_hash] = task
        task.add_done_callback(
            lambda _: None if stream.sd_hash not in self.running_reflector_uploads else
            self.running_reflector_uploads.pop(stream.sd_hash)
        )
        return task

    async def create_stream(self, file_path: str, key: Optional[bytes] = None,
                            iv_generator: Optional[typing.Generator[bytes, None, None]] = None) -> ManagedStream:
        stream = await ManagedStream.create(self.loop, self.config, self.blob_manager, file_path, key, iv_generator)
        self.streams[stream.sd_hash] = stream
        self.storage.content_claim_callbacks[stream.stream_hash] = lambda: self._update_content_claim(stream)
        if self.config.reflect_streams and self.config.reflector_servers:
            self.reflect_stream(stream)
        return stream

    async def delete_stream(self, stream: ManagedStream, delete_file: Optional[bool] = False):
        if stream.sd_hash in self.running_reflector_uploads:
            self.running_reflector_uploads[stream.sd_hash].cancel()
        stream.stop_tasks()
        if stream.sd_hash in self.streams:
            del self.streams[stream.sd_hash]
        blob_hashes = [stream.sd_hash] + [b.blob_hash for b in stream.descriptor.blobs[:-1]]
        await self.blob_manager.delete_blobs(blob_hashes, delete_from_db=False)
        await self.storage.delete(stream.descriptor)

    # @cache_concurrent
    # async def download_stream_from_uri(self, uri, exchange_rate_manager: 'ExchangeRateManager',
    #                                    timeout: Optional[float] = None,
    #                                    file_name: Optional[str] = None,
    #                                    download_directory: Optional[str] = None,
    #                                    save_file: Optional[bool] = None,
    #                                    resolve_timeout: float = 3.0,
    #                                    wallet: Optional['Wallet'] = None) -> ManagedStream:
    #     manager = self.wallet_manager
    #     wallet = wallet or manager.default_wallet
    #     timeout = timeout or self.config.download_timeout
    #     start_time = self.loop.time()
    #     resolved_time = None
    #     stream = None
    #     txo: Optional[Output] = None
    #     error = None
    #     outpoint = None
    #     if save_file is None:
    #         save_file = self.config.save_files
    #     if file_name and not save_file:
    #         save_file = True
    #     if save_file:
    #         download_directory = download_directory or self.config.download_dir
    #     else:
    #         download_directory = None
    #
    #     payment = None
    #     try:
    #         # resolve the claim
    #         if not URL.parse(uri).has_stream:
    #             raise ResolveError("cannot download a channel claim, specify a /path")
    #         try:
    #             response = await asyncio.wait_for(
    #                 manager.ledger.resolve(wallet.accounts, [uri]),
    #                 resolve_timeout
    #             )
    #             resolved_result = self._convert_to_old_resolve_output(manager, response)
    #         except asyncio.TimeoutError:
    #             raise ResolveTimeoutError(uri)
    #         except Exception as err:
    #             if isinstance(err, asyncio.CancelledError):
    #                 raise
    #             log.exception("Unexpected error resolving stream:")
    #             raise ResolveError(f"Unexpected error resolving stream: {str(err)}")
    #         await self.storage.save_claims_for_resolve([
    #             value for value in resolved_result.values() if 'error' not in value
    #         ])
    #         resolved = resolved_result.get(uri, {})
    #         resolved = resolved if 'value' in resolved else resolved.get('claim')
    #         if not resolved:
    #             raise ResolveError(f"Failed to resolve stream at '{uri}'")
    #         if 'error' in resolved:
    #             raise ResolveError(f"error resolving stream: {resolved['error']}")
    #         txo = response[uri]
    #
    #         claim = Claim.from_bytes(binascii.unhexlify(resolved['protobuf']))
    #         outpoint = f"{resolved['txid']}:{resolved['nout']}"
    #         resolved_time = self.loop.time() - start_time
    #
    #         # resume or update an existing stream, if the stream changed: download it and delete the old one after
    #         updated_stream, to_replace = await self._check_update_or_replace(outpoint, resolved['claim_id'], claim)
    #         if updated_stream:
    #             log.info("already have stream for %s", uri)
    #             if save_file and updated_stream.output_file_exists:
    #                 save_file = False
    #             await updated_stream.start(node=self.node, timeout=timeout, save_now=save_file)
    #             if not updated_stream.output_file_exists and (save_file or file_name or download_directory):
    #                 await updated_stream.save_file(
    #                     file_name=file_name, download_directory=download_directory, node=self.node
    #                 )
    #             return updated_stream
    #
    #         if not to_replace and txo.has_price and not txo.purchase_receipt:
    #             payment = await manager.create_purchase_transaction(
    #                 wallet.accounts, txo, exchange_rate_manager
    #             )
    #
    #         stream = ManagedStream(
    #             self.loop, self.config, self.blob_manager, claim.stream.source.sd_hash, download_directory,
    #             file_name, ManagedStream.STATUS_RUNNING, content_fee=payment,
    #             analytics_manager=self.analytics_manager
    #         )
    #         log.info("starting download for %s", uri)
    #
    #         before_download = self.loop.time()
    #         await stream.start(self.node, timeout)
    #         stream.set_claim(resolved, claim)
    #         if to_replace:  # delete old stream now that the replacement has started downloading
    #             await self.delete(to_replace)
    #
    #         if payment is not None:
    #             await manager.broadcast_or_release(payment)
    #             payment = None  # to avoid releasing in `finally` later
    #             log.info("paid fee of %s for %s", dewies_to_lbc(stream.content_fee.outputs[0].amount), uri)
    #             await self.storage.save_content_fee(stream.stream_hash, stream.content_fee)
    #
    #         self._sources[stream.sd_hash] = stream
    #         self.storage.content_claim_callbacks[stream.stream_hash] = lambda: self._update_content_claim(stream)
    #         await self.storage.save_content_claim(stream.stream_hash, outpoint)
    #         if save_file:
    #             await asyncio.wait_for(stream.save_file(node=self.node), timeout - (self.loop.time() - before_download),
    #                                    loop=self.loop)
    #         return stream
    #     except asyncio.TimeoutError:
    #         error = DownloadDataTimeoutError(stream.sd_hash)
    #         raise error
    #     except Exception as err:  # forgive data timeout, don't delete stream
    #         expected = (DownloadSDTimeoutError, DownloadDataTimeoutError, InsufficientFundsError,
    #                     KeyFeeAboveMaxAllowedError)
    #         if isinstance(err, expected):
    #             log.warning("Failed to download %s: %s", uri, str(err))
    #         elif isinstance(err, asyncio.CancelledError):
    #             pass
    #         else:
    #             log.exception("Unexpected error downloading stream:")
    #         error = err
    #         raise
    #     finally:
    #         if payment is not None:
    #             # payment is set to None after broadcasting, if we're here an exception probably happened
    #             await manager.ledger.release_tx(payment)
    #         if self.analytics_manager and (error or (stream and (stream.downloader.time_to_descriptor or
    #                                                              stream.downloader.time_to_first_bytes))):
    #             server = self.wallet_manager.ledger.network.client.server
    #             self.loop.create_task(
    #                 self.analytics_manager.send_time_to_first_bytes(
    #                     resolved_time, self.loop.time() - start_time, None if not stream else stream.download_id,
    #                     uri, outpoint,
    #                     None if not stream else len(stream.downloader.blob_downloader.active_connections),
    #                     None if not stream else len(stream.downloader.blob_downloader.scores),
    #                     None if not stream else len(stream.downloader.blob_downloader.connection_failures),
    #                     False if not stream else stream.downloader.added_fixed_peers,
    #                     self.config.fixed_peer_delay if not stream else stream.downloader.fixed_peers_delay,
    #                     None if not stream else stream.sd_hash,
    #                     None if not stream else stream.downloader.time_to_descriptor,
    #                     None if not (stream and stream.descriptor) else stream.descriptor.blobs[0].blob_hash,
    #                     None if not (stream and stream.descriptor) else stream.descriptor.blobs[0].length,
    #                     None if not stream else stream.downloader.time_to_first_bytes,
    #                     None if not error else error.__class__.__name__,
    #                     None if not error else str(error),
    #                     None if not server else f"{server[0]}:{server[1]}"
    #                 )
    #             )
# =======
#             self.running_reflector_uploads.pop().cancel()
#         super().stop()
#         log.info("finished stopping the stream manager")
# 
#     def _upload_stream_to_reflector(self, stream: ManagedStream):
#         if self.config.reflector_servers:
#             host, port = random.choice(self.config.reflector_servers)
#             task = self.loop.create_task(stream.upload_to_reflector(host, port))
#             self.running_reflector_uploads.append(task)
#             task.add_done_callback(
#                 lambda _: None
#                 if task not in self.running_reflector_uploads else self.running_reflector_uploads.remove(task)
#             )
# 
#     async def create(self, file_path: str, key: Optional[bytes] = None,
#                      iv_generator: Optional[typing.Generator[bytes, None, None]] = None) -> ManagedStream:
#         descriptor = await StreamDescriptor.create_stream(
#             self.loop, self.blob_manager.blob_dir, file_path, key=key, iv_generator=iv_generator,
#             blob_completed_callback=self.blob_manager.blob_completed
#         )
#         await self.storage.store_stream(
#             self.blob_manager.get_blob(descriptor.sd_hash), descriptor
#         )
#         row_id = await self.storage.save_published_file(
#             descriptor.stream_hash, os.path.basename(file_path), os.path.dirname(file_path), 0
#         )
#         source = ManagedStream(
#             self.loop, self.config, self.blob_manager, descriptor.sd_hash, os.path.dirname(file_path),
#             os.path.basename(file_path), status=ManagedDownloadSource.STATUS_FINISHED,
#             rowid=row_id, descriptor=descriptor
#         )
#         self.add(source)
#         if self.config.reflect_streams:
#             self._upload_stream_to_reflector(source)
#         return source
# 
#     async def _delete(self, stream: ManagedStream, delete_file: Optional[bool] = False):
# >>>>>>> ManagedDownloadSource and SourceManager refactor

    async def stream_partial_content(self, request: Request, sd_hash: str):
        return await self._sources[sd_hash].stream_file(request, self.node)
