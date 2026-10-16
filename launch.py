"""Start the loopback workspace independently of a Codex conversation."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

root = Path(__file__).resolve().parent
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--no-open', action='store_true', help='只启动服务；在 Codex 浏览器打开显示的 URL')
p.add_argument('--demo', action='store_true', help='仅启动当前目录的隔离项目图 Demo')
p.add_argument('--port', type=int, default=8766, help='本机监听端口')
args = p.parse_args()
url = f'http://127.0.0.1:{args.port}'

def healthy():
    try:
        with urllib.request.urlopen(url+'/api/session', timeout=.8) as response:
            data = json.load(response)
        if data.get('root') != str(root) or bool(data.get('demo_mode')) != args.demo:
            raise RuntimeError(f'端口 {args.port} 已被其他项目或模式占用；不接管或停止它。')
        return True
    except (urllib.error.URLError, TimeoutError):
        return False
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError(f'端口 {args.port} 返回了其他服务的内容；未接管它。') from exc

try:
    if not healthy():
        folder=root/'.project-graph'
        if not (folder/'project.json').exists():
            raise RuntimeError('未找到项目状态入口，不能自动初始化或覆盖。')
        log_name = 'demo-server.log' if args.demo else 'server.log'
        pid_name = 'demo-server.pid' if args.demo else 'server.pid'
        command = [sys.executable, str(root/'pgw.py'), 'serve', '--port', str(args.port)]
        if args.demo:
            command.append('--demo')
        with (folder/log_name).open('ab') as log:
            proc=subprocess.Popen(command,cwd=root,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        (folder/pid_name).write_text(str(proc.pid))
        for _ in range(40):
            if healthy():break
            if proc.poll() is not None:raise RuntimeError(f'服务启动失败，请查看 .project-graph/{log_name}')
            time.sleep(.05)
        else:raise RuntimeError('服务启动超时，保留日志，未重复启动。')
    print('项目工作区：'+url)
    print('状态独立保存在：'+str(root/'.project-graph'))
    print('在 Codex 中打开上述网址即可继续同一个项目。')
    if not args.no_open:webbrowser.open(url)
except (RuntimeError,OSError,ValueError) as e:
    print(str(e),file=sys.stderr);sys.exit(1)
