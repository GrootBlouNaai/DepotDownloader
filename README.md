# Steam Manifest File Download

## Global Parameters

* `-t, --thread-num`: Number of threads (default 32)
* `-o, --save-path`: Download path
* `-s, --server`: Specify CDN for download, can specify multiple or separate with commas
* `-l, --level`: Log level
* `-r, --retry-num`: Number of retries (default 3)

## Subcommand Parameters

* `app`: Download all manifests
    * `-p, --app-path`: Manifest directory
        * [Directory Structure](https://github.com/wxy1343/ManifestAutoUpdate/tree/10)
            * `*.manifest`: Manifest files
            * `config.vdf`: Key files
* `depot`: Download individual manifests
    * `-m, --manifest-path`: Manifest file path, can specify multiple or separate with spaces
    * `-k, --depot-key`: Depot key, can specify multiple or separate with spaces

## Usage Examples

* `python main.py app --app-path ./10`
* `python main.py depot --manifest-path "368010_6622130648560741481.manifest" --depot-key ef8ea30154f995c4e4226df06f5cc39705ef0fc2d800f948613d1b3dd6b6437e`

## Download Acceleration

1. Specify CDN for download
    * Usage example: `python main.py -s https://google.cdn.steampipe.steamcontent.com {app,depot} ...`
    * Specify multiple with commas, or use multiple `-s`
    * CDN List
        * Google
            * `https://google.cdn.steampipe.steamcontent.com`
            * `https://google2.cdn.steampipe.steamcontent.com`
        * Level3
            * `https://level3.cdn.steampipe.steamcontent.com`
        * Akamai
            * `https://steampipe.akamaized.net`
            * `https://steampipe-kr.akamaized.net`
            * `https://steampipe-partner.akamaized.net`
        * Kingsoft Cloud
            * `http://dl.steam.clngaa.com`
        * BaishanCloud
            * `http://st.dl.eccdnx.com`
            * `http://st.dl.bscstorage.net`
            * `http://trts.baishancdnx.cn`

2. Use tool: [UsbEAm Hosts Editor](https://www.dogfight360.com/blog/475/)

## Importing Old Manifests into Steam

* Steam cannot download old manifests for import
* Use this tool to download old manifest files to the Steam game directory
* Use [steamtools](https://steamtools.net/) to enable `Block Game Downloads and Updates`, then click to download an empty package to play the old version
