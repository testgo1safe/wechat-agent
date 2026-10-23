# -*- coding: utf-8 -*-
import sys
import time
import urllib.request

port = int(sys.argv[1]) if len(sys.argv) > 1 else 9222
url = f"http://127.0.0.1:{port}/json/version"
for i in range(15):
    try:
        urllib.request.urlopen(url, timeout=1)
        print("       端口 %d 已就绪 OK" % port)
        raise SystemExit(0)
    except Exception:
        print("  ...%ds" % (i + 1), flush=True)
        time.sleep(1)
print("       端口 %d 未就绪 FAIL" % port)
raise SystemExit(1)
