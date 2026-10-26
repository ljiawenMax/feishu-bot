"""一次性合并已核实的压缩衍生会话；默认只预览，--apply 才写库。

在线执行时必须在任务最终回复后重启服务：临时触发器让旧进程的最后一轮审计
落到原始行，db.migrate 在下次启动时清理触发器。不会删除任何磁盘会话文件。
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

from sqlalchemy import func, select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
from models import Conversation, Message
from ops.envfile import load


def serialize(row):
    return {col.name: getattr(row, col.name) for col in row.__table__.columns}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=int)
    parser.add_argument('--sources', required=True, nargs='+', type=int)
    parser.add_argument('--recover-sid', action='append', default=[])
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    chat_id = os.environ['FEISHU_CHAT_ID']
    if not re.fullmatch(r'oc_[0-9a-f]{32}', chat_id):
        raise ValueError('当前群 ID 无效')
    if args.root < 1 or not args.sources or min(args.sources) < 1 or args.root in args.sources:
        raise ValueError('原始行和来源行必须为不同的有效 ID')
    env = load('local')
    engine = db.init_engine(dict(host=env['DB_HOST'], port=int(env.get('DB_PORT', 3306)),
                                 dbname=env['DB_NAME'], user=env['DB_USER'], password=env['DB_PASSWORD']))
    factory = db.make_session_factory(engine)
    guard = 'fb_merge_guard_' + uuid.uuid4().hex[:12]
    try:
        with factory() as s:
            rows = s.scalars(select(Conversation).where(Conversation.chat_id == chat_id)
                             .order_by(Conversation.position, Conversation.id)).all()
            messages = s.scalars(select(Message).where(Message.chat_id == chat_id).order_by(Message.id)).all()
            records = {row.id: row for row in rows}
            required = {args.root} | set(args.sources)
            if not required <= records.keys():
                raise ValueError('原始会话或衍生会话不在当前群')
            if any(records[i].cwd != records[args.root].cwd for i in required):
                raise ValueError('工作目录不同，拒绝合并')
            moved = [m for m in messages if m.session_id in args.sources or
                     (m.session_id is None and m.claude_session_id in args.recover_sid)]
            plan = dict(chat_id=chat_id, root=args.root, root_label=records[args.root].label,
                        sources=sorted(set(args.sources)), recover_sids=args.recover_sid,
                        before_sessions=len(rows), after_sessions=len(rows)-len(set(args.sources)),
                        moved_messages=len(moved), total_messages=len(messages),
                        current_rows=[r.id for r in rows if r.is_current], guard=guard)
            snapshot = dict(plan=plan, sessions=[serialize(r) for r in rows],
                            messages=[serialize(m) for m in messages])
            # 老版本压缩摘要只存在 CLI 首轮 prompt 中，一并保存，便于核对来源和恢复。
            snapshot['legacy_compact_prompts'] = {}
            legacy_sids = set(args.recover_sid)
            legacy_sids.update(m.claude_session_id for m in moved if m.claude_session_id)
            base = Path.home() / '.claude/projects/-home-ljiawen-bot-work-dirs-general'
            for sid in sorted(legacy_sids):
                if not re.fullmatch(r'[0-9a-f-]{36}', sid):
                    continue
                path = base / (sid + '.jsonl')
                if not path.is_file():
                    continue
                with path.open() as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        content = (event.get('message') or {}).get('content')
                        if event.get('type') == 'user' and isinstance(content, str):
                            if '[以下是上一段对话的压缩摘要' in content:
                                snapshot['legacy_compact_prompts'][sid] = content
                            break
            fingerprint = snapshot['sessions']
        print(json.dumps(plan, ensure_ascii=False))
        if not args.apply:
            return
        folder = Path.home() / 'run/backups/feishu-bot' / ('session-merge-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
        folder.mkdir(parents=True, mode=0o700)
        os.chmod(folder, 0o700)
        backup = folder / 'before.json'
        with backup.open('x', encoding='utf-8') as f:
            os.chmod(backup, 0o600)
            json.dump(snapshot, f, ensure_ascii=False, default=str, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # DDL 单独执行；失败事务会撤掉触发器，避免对未合并的会话错误转发。
        sources_sql = ','.join(str(i) for i in sorted(set(args.sources)))
        with engine.begin() as c:
            c.execute(text(f"CREATE TRIGGER `{guard}` BEFORE INSERT ON messages FOR EACH ROW "
                           f"BEGIN IF NEW.chat_id='{chat_id}' AND NEW.session_id IN ({sources_sql}) "
                           f"THEN SET NEW.session_id={args.root}; END IF; END"))
        try:
            with factory.begin() as s:
                live = s.scalars(select(Conversation).where(Conversation.chat_id == chat_id)
                                 .order_by(Conversation.position, Conversation.id).with_for_update()).all()
                if [serialize(r) for r in live] != fingerprint:
                    raise RuntimeError('备份后会话状态变化，取消合并')
                if s.scalar(select(func.count()).select_from(Message).where(Message.chat_id == chat_id)) != len(messages):
                    raise RuntimeError('备份后消息数量变化，取消合并')
                result = db.merge_sessions(s, chat_id, args.root, args.sources, args.recover_sid)
                if result['moved_messages'] != len(moved):
                    raise RuntimeError('历史迁移数量不符，回滚')
                if s.scalar(select(func.count()).select_from(Conversation).where(Conversation.chat_id == chat_id)) != plan['after_sessions']:
                    raise RuntimeError('会话数量不符，回滚')
                if s.scalar(select(func.count()).select_from(Message).where(Message.chat_id == chat_id)) != len(messages):
                    raise RuntimeError('消息数量不符，回滚')
                remaining = s.scalar(select(func.count()).select_from(Message).where(Message.chat_id == chat_id,
                                      Message.session_id.in_(set(args.sources))))
                if remaining:
                    raise RuntimeError('仍有历史指向衍生条目，回滚')
        except BaseException:
            with engine.begin() as c:
                c.execute(text(f'DROP TRIGGER `{guard}`'))
            raise
        # 实际 MySQL 回滚事务验证旧进程的审计引用会被转发；不留下测试消息。
        with factory() as s:
            try:
                db.append_message(s, args.sources[-1], chat_id, None, 'test', 'merge-guard-rollback-test')
                s.flush()
                row = s.scalar(select(Message).where(Message.chat_id == chat_id, Message.role == 'test',
                                                     Message.content == 'merge-guard-rollback-test')
                               .order_by(Message.id.desc()).limit(1))
                if row.session_id != args.root:
                    raise RuntimeError('审计转发校验失败')
            finally:
                s.rollback()
        with factory() as s:
            restored = db.load_state(s, chat_id)['sessions']
            root = next(e for e in restored['list'] if e['_row_id'] == args.root)
            report = dict(result, backup=str(backup), backup_sha256=hashlib.sha256(backup.read_bytes()).hexdigest(),
                          sessions=len(restored['list']), current_row=restored['list'][restored['current']]['_row_id'],
                          root_label=root['label'], latest_execution_sid=root['id'], guard=guard,
                          verified_message_count=s.scalar(select(func.count()).select_from(Message).where(Message.chat_id == chat_id)),
                          verified_root_messages=s.scalar(select(func.count()).select_from(Message).where(Message.session_id == args.root)))
        report_path = folder / 'after.json'
        with report_path.open('x', encoding='utf-8') as f:
            os.chmod(report_path, 0o600)
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(json.dumps(report, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
