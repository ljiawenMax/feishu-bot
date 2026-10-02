"""Unix socket 工具服务。凭每轮短期令牌路由，群/消息与权限以 bot 快照为准。"""
import contextlib
import fcntl
import json
import os
import secrets
import socketserver
import stat
import subprocess
import threading
from pathlib import Path

from core import git as core_git
from core import uploads as core_uploads
import db
import feishu_api

ROOT = Path(__file__).resolve().parent
MAX_REQUEST = 65536
_SERVER = None


class CallContext:
    def __init__(self, bot, msg_id, socket_path, entry=None, unsafe=None):
        self.bot, self.msg_id = bot, msg_id
        self.chat_id = bot.chat_id
        self.cwd = bot.entry_cwd(entry) if entry is not None else bot.current_cwd()
        self.unsafe = bot.unsafe_mode if unsafe is None else unsafe
        self.restart = self.compact = False
        self.files = 0
        self.lock = threading.Lock()
        self.env = {'FEISHU_CHAT_ID': self.chat_id, 'FEISHU_MSG_ID': msg_id,
                    'FEISHU_BOT_SOCK': str(socket_path), 'FEISHU_TOOL_TOKEN': secrets.token_hex(32),
                    'FEISHU_TOOL_CLI': str(ROOT / 'fbtool'),
                    'FEISHU_DEBATE_SCRIPT': str(ROOT / '.agents/skills/feishu-debate/scripts/debate.py')}

    def dispatch(self, operation, args):
        bot, msg_id = self.bot, self.msg_id
        if operation == 'send_text':
            text = args['text']
            if not isinstance(text, str) or not text.strip() or len(text) > 12000:
                raise ValueError('文本必须非空，且不超过 12000 字')
            # 进展文字不能夹带附件标记；附件走独立且计数的入口。
            if core_uploads.SEND_FILE_RE.search(text):
                raise ValueError('请用 send_file 发送附件')
            if not bot._push_reply(msg_id, text, tag='tool-progress'):
                raise RuntimeError('飞书消息发送失败')
            return {'sent': True}
        if operation in ('send_file', 'send_image'):
            path = core_uploads.validate_path(args['path'])
            if self.files >= core_uploads.MAX_FILES:
                raise ValueError('本轮最多通过工具发送 3 个附件')
            # 消耗名额后再发送，网络失败不自动重发，避免重复外发。
            self.files += 1
            if operation == 'send_image':
                result = feishu_api.reply_image_path(bot.client, msg_id, path)
            else:
                key = feishu_api.upload_file(bot.client, path, os.path.basename(path))
                result = feishu_api.reply_file(bot.client, msg_id, key, tag='tool-file')
            bot._audit_sends(msg_id, operation, result)
            if not result:
                raise RuntimeError('飞书附件发送失败')
            return {'sent': True, 'path': path}
        if operation == 'restart_service':
            if not self.unsafe or not bot.unsafe_mode:
                raise ValueError('重启服务需要用户先开启 /unsafe')
            self.restart = True
            return {'pending': True, 'message': '最终回复成功送达后重启；未送达或任务失败则取消'}
        if operation == 'compact_context':
            self.compact = True
            return {'pending': True, 'message': '本轮执行完成后生成摘要，下一轮使用新上下文'}
        if operation == 'wiki_search':
            if not bot.wiki_space_id:
                return {'reference': '', 'message': '本群未绑定知识库'}
            query = args['query']
            if not isinstance(query, str) or not query.strip() or len(query) > 2000:
                raise ValueError('检索问题必须非空，且不超过 2000 字')
            return {'reference': bot._wiki_reference(query, strict=True), 'status': bot.wiki_status}
        if operation == 'history_search':
            query = args['query']
            if not isinstance(query, str) or not query.strip() or len(query) > 200:
                raise ValueError('关键词必须非空，且不超过 200 字')
            messages = bot.db(db.search_history, self.chat_id, query)
            if messages is None:
                raise RuntimeError('历史查询失败，请查看数据库连接')
            return {'messages': messages}
        if operation == 'list_uploads':
            limit = args.get('limit', 10)
            if not isinstance(limit, int) or not 1 <= limit <= 50:
                raise ValueError('limit 必须在 1～50 之间')
            items = bot.db(db.list_uploads, self.chat_id, limit)
            if items is None:
                raise RuntimeError('上传台账查询失败，请查看数据库连接')
            return {'uploads': items, 'pending': bot.pending_uploads_snapshot()}
        if operation == 'git_local_changes':
            return core_git.local_changes(self.cwd)
        raise ValueError(f'未知工具：{operation}')


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(30)
        try:
            raw = self.rfile.readline(MAX_REQUEST + 1)
            if len(raw) > MAX_REQUEST or not raw.endswith(b'\n'):
                raise ValueError('请求超过 64KB 或不完整')
            req = json.loads(raw)
            if not isinstance(req, dict) or not isinstance(req.get('arguments'), dict):
                raise ValueError('请求格式错误')
            ctx = self.server.lookup(req.get('token'))
            if (not ctx or req.get('chat_id') != ctx.chat_id or req.get('msg_id') != ctx.msg_id):
                raise ValueError('任务令牌失效或群/消息不匹配')
            # 同轮工具串行，防并发绕过附件数量限制。
            with ctx.lock:
                if self.server.lookup(req.get('token')) is not ctx:
                    raise ValueError('任务已结束')
                result = ctx.dispatch(req.get('operation'), req['arguments'])
            response = {'ok': True, 'result': result}
        except Exception as e:
            response = {'ok': False, 'error': str(e)}
        try:
            self.wfile.write((json.dumps(response, ensure_ascii=False) + '\n').encode())
        except OSError:
            pass


class ToolServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path):
        self.contexts = {}
        self.context_lock = threading.Lock()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 持有锁直到关闭，第二个实例不可删除运行中的 socket。
        self.lock_file = open(str(path) + '.lock', 'a')
        os.chmod(str(path) + '.lock', 0o600)
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if path.exists() or path.is_symlink():
                if not stat.S_ISSOCK(path.lstat().st_mode):
                    raise ValueError("工具 socket 路径已被普通文件或链接占用")
                path.unlink()
            # 避免 bind 与 chmod 之间的可访问窗口。
            old_umask = os.umask(0o077)
            try:
                super().__init__(str(path), Handler)
            finally:
                os.umask(old_umask)
            os.chmod(path, 0o600)
        except Exception:
            self.lock_file.close()
            raise

    def lookup(self, token):
        if not isinstance(token, str):
            return None
        with self.context_lock:
            return self.contexts.get(token)

    def server_close(self):
        super().server_close()
        Path(self.server_address).unlink(missing_ok=True)
        self.lock_file.close()


def start(path=None):
    global _SERVER
    path = path or str(Path.home() / 'run' / 'feishu_bot' / 'tools.sock')
    _SERVER = ToolServer(path)
    threading.Thread(target=_SERVER.serve_forever, name='fbtool-server', daemon=True).start()
    return _SERVER


@contextlib.contextmanager
def bind(bot, msg_id, entry=None, unsafe=None):
    server = _SERVER
    ctx = CallContext(bot, msg_id, server.server_address if server else '', entry, unsafe)
    if server:
        with server.context_lock:
            server.contexts[ctx.env['FEISHU_TOOL_TOKEN']] = ctx
    try:
        yield ctx
    finally:
        if server:
            # 等待本轮已开始的工具完成后再失效；失效后不能外发。
            with ctx.lock, server.context_lock:
                server.contexts.pop(ctx.env['FEISHU_TOOL_TOKEN'], None)


def schedule_restart():
    """只在最终回复送达后调用，在当前 cgroup 之外调度。"""
    unit = 'feishu-bot-restart-' + secrets.token_hex(6)
    subprocess.run(['sudo', '-n', 'systemd-run', '--collect', '--no-block', '--unit=' + unit,
                    '/bin/sh', '-c', 'sleep 2; systemctl restart feishu-bot.service'],
                   check=True, capture_output=True, text=True, timeout=15)
    return unit
