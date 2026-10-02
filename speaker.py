"""独立语音 CLI 能力：Azure TTS → SSH 传整段 WAV → 指定 sink 同步播放。"""
import fcntl
import json
import os
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

MAX_CHARS = 200


def synthesize(text, config):
    region = config.get('region', 'japaneast')
    if not re.fullmatch(r'[a-z0-9-]+', region):
        raise ValueError('Azure region 配置无效')
    voice = escape(config.get('voice', 'zh-CN-XiaoxiaoNeural'), {'"': '&quot;'})
    body = ('<speak version="1.0" xml:lang="zh-CN"><voice name="' + voice + '">' +
            escape(text) + '</voice></speak>').encode()
    request = urllib.request.Request(
        f'https://{region}.tts.speech.microsoft.com/cognitiveservices/v1', data=body,
        headers={'Ocp-Apim-Subscription-Key': config['key'], 'Content-Type': 'application/ssml+xml',
                 'X-Microsoft-OutputFormat': 'riff-24khz-16bit-mono-pcm', 'User-Agent': 'feishu-bot'})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                audio = response.read(8 * 1024 * 1024 + 1)
            if len(audio) > 8 * 1024 * 1024 or not audio.startswith(b'RIFF'):
                raise RuntimeError('Azure 未返回有效 WAV 音频')
            return audio
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise RuntimeError(f'Azure TTS HTTP {e.code}') from None
            if attempt == 3:
                raise RuntimeError('Azure TTS 429 限流，退避重试后仍失败') from None
            try:
                delay = float(e.headers.get('Retry-After', 2 ** attempt))
            except (TypeError, ValueError):
                delay = 2 ** attempt
            time.sleep(min(max(delay, 0.5), 10))


def play(audio, config):
    remote, sink = config.get('remote', ''), config.get('sink', '')
    if not re.fullmatch(r'[A-Za-z0-9_.@:-]+', remote) or remote.startswith('-') or not sink:
        raise ValueError('请配置 SSH 主机 remote 与音箱 sink')
    # 先完整落盘，再查 sink，缺失明确 rc=99 且不退回默认声卡。
    script = '\n'.join([
        'export XDG_RUNTIME_DIR=/run/user/$(id -u)',
        'export PULSE_RUNTIME_PATH="$XDG_RUNTIME_DIR/pulse"',
        'f=$(mktemp /tmp/feishu-speak-XXXXXX.wav) || exit 97',
        'trap \'rm -f "$f"\' EXIT',
        'cat > "$f" || exit 98',
        "pactl list short sinks | awk '{print $2}' | grep -qxF -- " + shlex.quote(sink) + ' || exit 99',
        'paplay --volume=65536 --device=' + shlex.quote(sink) + ' "$f"',
    ])
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', remote,
                             'sh -c ' + shlex.quote(script)], input=audio,
                            capture_output=True, timeout=120)
    if result.returncode == 99:
        raise RuntimeError('音箱 sink 不存在（rc=99），请检查蓝牙连接')
    if result.returncode:
        detail = result.stderr.decode(errors='replace').strip()[:300]
        raise RuntimeError(f'音箱播放失败 rc={result.returncode}：{detail}')


def speak(text, config_path=None):
    if not isinstance(text, str) or not text.strip():
        raise ValueError('朗读文本不能为空')
    text = text.strip()
    truncated = len(text) > MAX_CHARS
    text = text[:MAX_CHARS]
    config_path = config_path or os.environ.get('FEISHU_SPEAKER_CONFIG') or str(
        Path.home() / '.config' / 'feishu-bot' / 'speaker.json')
    try:
        config = json.loads(Path(config_path).read_text())
    except FileNotFoundError:
        raise ValueError('未配置语音：需要 ~/.config/feishu-bot/speaker.json') from None
    if not config.get('key'):
        raise ValueError('语音配置缺少 Azure TTS key')
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
    if not runtime.is_dir():
        runtime = Path.home() / 'run' / 'feishu_bot'
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(runtime / 'feishu-speaker.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'w') as lock:
        # 限时等待，锁覆盖合成与播放。超时不播放，避免任务取消后突然出声。
        deadline = time.monotonic() + 120
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('音箱忙，等待 120 秒后超时')
                time.sleep(0.1)
        play(synthesize(text, config), config)
    return {'played': True, 'characters': len(text), 'truncated': truncated,
            'message': '已截断至前 200 字' if truncated else '播放完成'}
