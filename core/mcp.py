"""标准库 MCP stdio 适配器：由 Codex 主进程调用受任务令牌保护的工具。"""
import json

TOOL_FIELDS = {
    'debate': ('topic', '辩题（最多2000字）；独立正反双方各五轮，逐条回复当前消息'),
    'send_text': ('text', '向当前飞书消息回复一段进展文字'),
    'send_file': ('path', '发送一个已存在的绝对路径文件，最多20MB，本轮最多3个'),
    'send_image': ('path', '发送一个已存在的绝对路径图片，本轮最多3个附件'),
    'wiki_search': ('query', '检索当前群绑定的知识库'),
    'history_search': ('query', '检索当前群数据库历史'),
}
EMPTY_TOOLS = {
    'restart_service': '仅登记重启，用户已开启 /unsafe 才可用；最终回复送达后执行',
    'compact_context': '登记当前上下文压缩，本轮结束后执行',
    'git_local_changes': '只读查看当前任务目录的 git 改动',
}


def definitions():
    tools = []
    for name, (field, description) in TOOL_FIELDS.items():
        tools.append({'name': name, 'description': description,
                      'inputSchema': {'type': 'object', 'properties': {field: {'type':'string'}},
                                      'required': [field], 'additionalProperties': False}})
    for name, description in EMPTY_TOOLS.items():
        tools.append({'name': name, 'description': description,
                      'inputSchema': {'type':'object', 'properties':{}, 'additionalProperties':False}})
    tools.append({'name':'list_uploads', 'description':'查询本群上传台账和待用附件，不消费附件',
                  'inputSchema': {'type':'object', 'properties':{'limit':{'type':'integer','minimum':1,'maximum':50}},
                                  'additionalProperties':False}})
    return tools


def serve(call, source, sink):
    allowed = {tool['name'] for tool in definitions()}
    while True:
        line = source.readline(1024 * 1024 + 1)
        if not line:
            return
        req_id = None
        try:
            if len(line) > 1024 * 1024 or not line.endswith('\n'):
                raise ValueError('MCP 请求不完整或过大')
            req = json.loads(line)
            if not isinstance(req, dict) or req.get('jsonrpc') != '2.0':
                raise ValueError('MCP 请求格式错误')
            if 'id' not in req:  # initialized/cancelled 等通知不需要回复。
                continue
            req_id = req['id']
            method, params = req.get('method'), req.get('params', {})
            if not isinstance(params, dict):
                raise ValueError('MCP 参数必须为对象')
            if method == 'initialize':
                result = {'protocolVersion':'2024-11-05', 'capabilities':{'tools':{}},
                          'serverInfo':{'name':'feishu-bot', 'version':'1.0.0'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': definitions()}
            elif method == 'tools/call':
                name, arguments = params.get('name'), params.get('arguments', {})
                if name not in allowed or not isinstance(arguments, dict):
                    raise ValueError('未知工具或参数格式错误')
                try:
                    response = call(name, arguments)
                except Exception as e:
                    response = {'ok':False, 'error':str(e)}
                result = {'content':[{'type':'text', 'text':json.dumps(response, ensure_ascii=False)}],
                          'isError':not response.get('ok', False)}
            else:
                sink.write(json.dumps({'jsonrpc':'2.0','id':req_id,
                                       'error':{'code':-32601,'message':'未知 MCP 方法'}})+'\n')
                sink.flush()
                continue
            response = {'jsonrpc':'2.0','id':req_id,'result':result}
        except (ValueError, TypeError) as e:
            response = {'jsonrpc':'2.0','id':req_id,'error':{'code':-32602,'message':str(e)}}
        sink.write(json.dumps(response, ensure_ascii=False)+'\n')
        sink.flush()
