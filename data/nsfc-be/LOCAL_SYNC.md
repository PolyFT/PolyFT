# 本地同步

在仓库根目录执行：

```bash
python tools/sync_nsfc_be_local.py --dest data-local/nsfc-be
```

脚本会下载B口基础数据与B/E矩阵两个Release，核验SHA-256并原子更新本地目录。
