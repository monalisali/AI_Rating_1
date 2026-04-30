from modelscope import snapshot_download
snapshot_download('Xorbits/bge-m3', cache_dir='./bge_m3_cache')
print("下载完成")
