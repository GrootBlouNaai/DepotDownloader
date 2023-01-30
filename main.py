import vdf
import time
import lzma
import json
import struct
import logging
import argparse
from tqdm import tqdm
from io import BytesIO
from pathlib import Path
from binascii import crc32
from zipfile import ZipFile
from collections import deque
from urllib.parse import urljoin
from steam.exceptions import SteamError
from requests.adapters import HTTPAdapter
from multiprocessing.pool import ThreadPool
from multiprocessing.dummy import Pool, Lock
from steam.core.manifest import DepotManifest
from steam.core.crypto import symmetric_decrypt
from steam.utils.web import make_requests_session
from steam.client.cdn import get_content_servers_from_webapi

lock = Lock()
parser = argparse.ArgumentParser(add_help=True)
parser.add_argument('-t', '--thread-num', default=32)
parser.add_argument('-o', '--save-path')
parser.add_argument('-s', '--server', dest='server_list', action='append', nargs='?')
parser.add_argument('-l', '--level', default='INFO')
parser.add_argument('-r', '--retry-num', type=int, default=3)

subparsers = parser.add_subparsers(dest='command', required=True)

app_parser = subparsers.add_parser('app')
app_parser.add_argument('-p', '--app-path', required=True)

depot_parser = subparsers.add_parser('depot')
depot_parser.add_argument('-m', '--manifest-path', dest='manifest_path_list', action='extend', nargs='+', required=True)
depot_parser.add_argument('-k', '--depot-key', dest='depot_key_list', action='extend', nargs='+', required=True)


class ChunkDownload:
    def __init__(self, depot_downloader, mapping):
        self.depot_downloader = depot_downloader
        self.tqdm: tqdm = self.depot_downloader.tqdm
        self.manifest = self.depot_downloader.manifest
        self.mapping = mapping
        self.download_size = 0
        self.chunk_dict = self.depot_downloader.chunk_dict
        self.chunk_list_path = self.depot_downloader.chunk_list_path
        self.depot_id = self.depot_downloader.depot_id
        self.depot_key = self.depot_downloader.depot_key
        self.log = self.depot_downloader.log
        self.filepa = self.mapping.filename.replace('\\', '/')
        self.path = self.depot_downloader.save_path / self.filepa

    def download(self, chunk):
        chunk_id = chunk.sha.hex()
        data = self.get_chunk(chunk_id)
        with lock:
            self.download_size += chunk.cb_original
            self.depot_downloader.total_size += chunk.cb_original
            self.log.debug(
                f'{self.path} {chunk_id} {self.download_size / self.mapping.size * 100:.2f}%/{self.depot_downloader.total_size / self.manifest.metadata.cb_disk_original * 100:.2f}%')
            with self.path.open('rb+') as f:
                f.seek(chunk.offset, 0)
                f.write(data)
            self.chunk_dict[self.filepa].append(f'{chunk.offset}_{chunk.sha.hex()}')
        self.tqdm.set_postfix(filename=self.mapping.filename)
        self.tqdm.update(chunk.cb_original)

    def get_chunk(self, chunk_id):
        server = self.depot_downloader.get_content_server()

        while True:
            url = urljoin(server, f'depot/{self.depot_id}/chunk/{chunk_id}')
            try:
                resp = self.depot_downloader.web.get(url, timeout=10)
            except Exception as exp:
                self.log.debug("%s %S Request error: %s", self.path, chunk_id, exp)
            else:
                if resp.ok:
                    break
                elif 400 <= resp.status_code < 500:
                    self.log.debug("%s %s Got HTTP ", self.path, chunk_id, resp.status_code)
                    raise SteamError("%s %s HTTP Error %s" % (self.path, chunk_id, resp.status_code))
                time.sleep(0.5)
            server = self.depot_downloader.get_content_server(rotate=True)

        data = symmetric_decrypt(resp.content, bytes.fromhex(self.depot_key))

        if data[:2] == b'VZ':
            if data[-2:] != b'zv':
                raise SteamError("%s %s VZ: Invalid footer: %s" % (self.path, chunk_id, repr(data[-2:])))
            if data[2:3] != b'a':
                raise SteamError("%s %s VZ: Invalid version: %s" % (self.path, chunk_id, repr(data[2:3])))

            vzfilter = lzma._decode_filter_properties(lzma.FILTER_LZMA1, data[7:12])
            vzdec = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[vzfilter])
            checksum, decompressed_size = struct.unpack('<II', data[-10:-2])
            # decompress_size is needed since lzma will sometime produce longer output
            # [12:-9] is need as sometimes lzma will produce shorter output
            # together they get us the right data
            data = vzdec.decompress(data[12:-9])[:decompressed_size]
            if crc32(data) != checksum:
                raise SteamError("%s %s VZ: CRC32 checksum doesn't match for decompressed data" % (self.path, chunk_id))
        else:
            with ZipFile(BytesIO(data)) as zf:
                data = zf.read(zf.filelist[0])

        return data

    def error_callback(self, e):
        self.log.error(e)


class DepotDownloader:
    def __init__(self, manifest_path, depot_key, thread_num=32, save_path=None, servers=None,
                 level=logging.INFO, retry_num=3):
        self.manifest_path = manifest_path
        self.depot_key = depot_key
        self.thread_num = thread_num
        self.total_size = 0
        self.log = logging.getLogger(self.__class__.__name__)
        logging.basicConfig(format='%(asctime)s - %(pathname)s[line:%(lineno)d] - %(levelname)s: %(message)s',
                            level=level)
        self.servers = deque()
        self.get_content_server(servers)
        with open(self.manifest_path, 'rb') as f:
            content = f.read()
        self.manifest = DepotManifest(content)
        self.depot_id = self.manifest.depot_id
        self.chunk_list_path = Path(f'{self.depot_id}.json')
        self.save_path = Path(save_path) if save_path else Path(str(self.depot_id))
        self.chunk_dict = {}
        if self.chunk_list_path.exists():
            with self.chunk_list_path.open() as f:
                self.chunk_dict = json.load(f)
        self.web = make_requests_session()
        adapters = HTTPAdapter(max_retries=retry_num, pool_connections=10000, pool_maxsize=10000)
        self.web.mount('http://', adapters)
        self.web.mount('https://', adapters)
        self.tqdm = tqdm(total=self.manifest.metadata.cb_disk_original, unit='B', unit_scale=True)
        self.tqdm.set_description_str(f'Depot {self.depot_id}')

    def get_content_server(self, servers=None, rotate=True):
        if servers:
            self.servers.extend(servers)
        if not self.servers:
            self.log.debug("Trying to fetch content servers from Steam API")
            self.servers.extend([f"{'https' if server.https else 'http'}://{server.host}:{server.port}" for server in
                                 filter(lambda server: server.type != 'OpenCache',
                                        get_content_servers_from_webapi(b'0'))])
        if not self.servers:
            raise SteamError("Failed to fetch content servers")
        if rotate:
            self.servers.rotate(-1)
        return self.servers[0]

    def save_chunk_dict(self):
        with lock:
            with open(self.chunk_list_path, 'w') as f:
                json.dump(self.chunk_dict, f)

    def download(self):
        result_list = []
        with Pool(int(self.thread_num)) as pool:
            pool: ThreadPool
            for mapping in self.manifest.payload.mappings:
                mapping.chunks.sort(key=lambda x: x.offset)
                d = ChunkDownload(self, mapping)
                filepa = mapping.filename.replace('\\', '/')
                path = self.save_path / filepa
                if mapping.flags != 64:
                    if not path.exists():
                        if filepa in self.chunk_dict:
                            self.chunk_dict[filepa] = []
                            self.save_chunk_dict()
                        if not path.parent.exists():
                            path.parent.mkdir(parents=True, exist_ok=True)
                        if not path.exists():
                            path.touch(exist_ok=True)
                if filepa not in self.chunk_dict:
                    self.chunk_dict[filepa] = []
                for chunk in mapping.chunks:
                    if f'{chunk.offset}_{chunk.sha.hex()}' not in self.chunk_dict[filepa]:
                        result_list.append(
                            pool.apply_async(d.download, (chunk,), error_callback=d.error_callback))
                    else:
                        with lock:
                            self.total_size += chunk.cb_original
                        self.tqdm.update(chunk.cb_original)
            try:
                while pool._state == 'RUN':
                    if all([result.ready() for result in result_list]):
                        break
                    self.save_chunk_dict()
                    time.sleep(0.1)
            except KeyboardInterrupt:
                pass
            finally:
                with lock:
                    pool.terminate()
                self.save_chunk_dict()


def get_manifest_path_depot_key_dict(path):
    path = Path(path)
    if not path.is_dir():
        raise NotADirectoryError(path)
    manifest_path_list = []
    depot_dict = {}
    for file in path.iterdir():
        if file.is_file():
            if file.suffix == '.manifest':
                manifest_path_list.append(file)
            elif file.name == 'config.vdf':
                with file.open() as f:
                    d = vdf.load(f)
                depots = d.get('depots')
                if not depots:
                    return {}
                for depot_id in depots:
                    depot_key = depots[depot_id].get('DecryptionKey')
                    if not depot_key:
                        continue
                    depot_dict[int(depot_id)] = depot_key
    manifest_path_depot_key_dict = {}
    for manifest_path in manifest_path_list:
        with manifest_path.open('rb') as f:
            content = f.read()
        manifest = DepotManifest(content)
        if manifest.depot_id not in depot_dict:
            continue
        depot_key = depot_dict[manifest.depot_id]
        manifest_path_depot_key_dict[manifest_path] = depot_key
    return manifest_path_depot_key_dict


def main(args=None):
    if args:
        args = parser.parse_args(args)
    else:
        args = parser.parse_args()
    if args.level:
        level = logging.getLevelName(args.level.upper())
    else:
        level = logging.INFO
    manifest_path_depot_key_dict = {}
    save_path = args.save_path
    if args.command == 'app':
        manifest_path_depot_key_dict = get_manifest_path_depot_key_dict(args.app_path)
        if manifest_path_depot_key_dict and args.app_path and not save_path:
            save_path = Path().absolute() / Path(args.app_path).name
    elif args.command == 'depot':
        manifest_path_depot_key_dict = dict(zip(args.manifest_path_list, args.depot_key_list))
    server_set = set()
    if args.server_list:
        for server in args.server_list:
            server_set.update(server.split(','))
    if manifest_path_depot_key_dict:
        for manifest_path, depot_key in manifest_path_depot_key_dict.items():
            if manifest_path and depot_key:
                DepotDownloader(manifest_path, depot_key, args.thread_num, save_path, server_set, level,
                                args.retry_num).download()


if __name__ == '__main__':
    main()
