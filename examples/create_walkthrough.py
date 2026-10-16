"""Create a synthetic notification-release story through the real Store API.
No model calls, real chat IDs, approval decisions or existing-project writes.
Run from the application checkout: python3 examples/create_walkthrough.py /new/path
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import Store

TASKS = [
('N01','写清发布通知的范围',[],'仅通知订阅用户；默认关闭。范围原件必须写明退订入口与未覆盖渠道。'),
('N02','登记邮件与站内信渠道',['N01'],'首版采用站内信，邮件保留为候选。这里是演示决策，不代表真实产品选择。'),
('N03','保存通知载荷示例',['N01'],'字段：release_id、title、link、created_at。检查对象是合成 JSON，不能据此认定接口已经实现。'),
('N04','设计订阅接口与失败返回',['N03'],'保留现有订阅数据；写清重复提交、未授权和连接失败时的返回。实现尚未验证。'),
('N05','实现站内通知投递',['N04'],'依赖订阅接口稳定；需要验证重复投递和失败恢复。当前没有通过证据。'),
('N06','处理重试与重复投递',['N04'],'重试不得增加重复通知；真实并发与故障注入测试尚未执行。'),
('N07','设计订阅设置入口',['N02','N03'],'在设置页解释当前选择和保存结果；长中文说明、键盘和错误恢复待验证。'),
('N08','校对通知标题和长文本',['N02'],'标题应直接说明发布版本，正文保留完整链接。长路径示例：/releases/2026/notifications/首次发布说明.md。'),
('N09','记录投递结果与追踪依据',['N03'],'记录来源版本、投递结果与错误原因；不要将发送请求等同于送达。'),
('N10','核对订阅到送达的完整流程',['N05','N06','N07'],'必须验证成功、失败、重复与取消订阅；依赖尚未满足，不能提前通过。'),
('N11','核对退订与数据保留',['N02'],'隐私说明已有新版本，旧登记不能证明当前内容。请回读原件，验证状态保留未知。'),
('N12','准备回退与保留新增订阅',['N03'],'先保存当前状态，再验证回退不会覆盖后来新增订阅；目前未执行恢复演练。'),
('N13','整理发布说明与已知限制',['N01'],'草稿已登记为产物。真实功能与用户验收未完成，不能把草稿称为已发布。'),
('N14','人工核对本次发布范围',['N10','N11','N12'],'发布前须人工确认准确范围、未验证事项和回退方案。演示只展示待处理，不作决定。'),
('N15','执行获准的发布',['N14'],'只有对应人工确认与前置验证成立后才能开始。当前不执行发布。'),
('N16','评估后续邮件渠道',['N15'],'后续候选，当前暂缓；不把邮件能力写入首版已交付范围。')]

def create(target):
    root=Path(target).expanduser().resolve()
    if root.exists(): raise SystemExit('Refusing existing directory: choose a new demonstration project path.')
    root.mkdir(parents=True)
    content={
      'scope.md':'# 发布通知 · 合成项目\n\n本目录所有内容仅用于演示。\n首版：站内信；默认关闭；提供退订入口。\n未覆盖：真实邮件服务、多租户权限、生产发布。\n',
      'channels.md':'# 渠道记录\n\n选择站内信；邮件暂缓。原因：先验证订阅与送达闭环。\n未发生真实发送或用户批准。\n',
      'payload.json':json.dumps({'release_id':'demo-0.1','title':'发布通知示例','link':'/releases/demo-0.1','created_at':'2026-10-01T09:00:00+08:00'},ensure_ascii=False,indent=2)+'\n',
      'privacy.md':'# 隐私与退订 · 版本 1\n\n仅登记退订入口；保留周期尚未明确。合成材料。\n',
      'release-notes.md':'# 发布说明草稿\n\n站内通知功能仍在规划与验证中。未发布、未获人工验收。\n',
      'interface-sketch.md':'# 订阅接口草案\n\nPUT /subscriptions/releases\n幂等标识与失败返回尚待核定；没有已运行服务。\n'}
    for name,body in content.items(): (root/name).write_text(body,encoding='utf-8')
    store=Store(root);store.init('发布通知 · 合成演示','跨对话接续一个发布项目：找到可做任务，解释阻塞，回读依据；所有内容均为合成示例。')
    count=0
    def call(op,**args):
        nonlocal count
        count+=1
        return store.call(op,{**args,'actor':'walkthrough-fixture','request_id':f'walkthrough-{count}'})
    def node(id):return store.call('graph_get',{'id':id})['node']
    def change(op,id,**args):return call(op,id=id,expected_version=node(id)['version'],**args)
    def source(name):return {'uri':str(root/name),'locator':'全文；合成演示材料'}
    for id,title,deps,body in TASKS:call('task_create',id=id,title=title,body=body,depends_on=deps,checks=['fixture-content' if id in ['N01','N02','N03'] else 'technical'])
    for id,title,ids in [('P01','范围与依据',['N01','N02','N03']),('P02','实现与验证',['N04','N05','N06','N07','N08','N09','N10','N11','N12','N13']),('P03','发布与后续',['N14','N15','N16'])]:
        call('node_create',kind='Phase',id=id,title=title,body='合成项目阶段；任务状态以真实 Store 计算为准。')
        for tid in ids:call('relation_add',source_id=id,relation='contains',target_id=tid,basis='演示计划的阶段归属')
    for id,title,name in [('E01','范围原件 · 站内信与退订','scope.md'),('E02','渠道选择依据','channels.md'),('E03','通知载荷字段样例','payload.json'),('E04','隐私说明 · 登记版本已变化','privacy.md')]:call('evidence_add',id=id,title=title,body='合成演示依据，不证明真实业务实现或验收。',source=source(name))
    for tid,eid in [('N01','E01'),('N02','E02'),('N03','E03'),('N11','E04')]:call('relation_add',source_id=tid,relation='reads',target_id=eid,basis='任务需要核对该原件')
    for id,title,name,tid in [('A01','订阅接口草案 · 尚未实现','interface-sketch.md','N04'),('A02','发布说明草稿 · 未发布','release-notes.md','N13')]:call('artifact_register',id=id,title=title,body='真实存在的合成文件；产物登记不代表任务完成。',source=source(name),task_id=tid)
    call('decision_record',id='D01',title='首版先验证站内通知',body='合成项目选择，非用户对实际项目的决定。',alternatives=['邮件渠道暂缓，待站内闭环通过再评估'],evidence_ids=['E01','E02'],revisit_when='有真实送达与用户体验证据后')
    call('decision_record',id='D02',title='发布前保留人工范围核对',body='技术验证与人工发布决定分开；演示不批准此门禁。',alternatives=['自动发布不采用，因当前前置与人工决定尚未满足'],evidence_ids=['E01'],revisit_when='用户明确修改发布授权边界时')
    call('idea_capture',id='I01',title='增加消息中心的筛选',body='未纳入当前范围；需要用户研究后再决定。')
    call('human_gate_request',id='G01',title='本次发布范围待人工确认',task_id='N14',body='核对准确版本、前置验证和回退方案；本演示不作批准或拒绝。')
    for id,name in [('N01','scope.md'),('N02','channels.md'),('N03','payload.json')]:
        change('task_claim',id)
        data=(root/name).read_text(encoding='utf-8');assert data.strip()
        if name=='payload.json':assert set(json.loads(data))=={'release_id','title','link','created_at'}
        receipt=root/(id+'-fixture-check.json');receipt.write_text(json.dumps({'scope':'synthetic file exists; payload keys only; no real business verification','file':name,'sha256':hashlib.sha256((root/name).read_bytes()).hexdigest(),'result':'PASS'},ensure_ascii=False,indent=2),encoding='utf-8')
        change('validation_record',id,check='fixture-content',result='PASS',body='实际检查合成文件存在与样例字段；不证明业务功能。',source=source(receipt.name),files=[source(name)])
        change('task_complete',id)
    for id in ['N04','N07']:change('task_claim',id)
    for id in ['N11','N13']:change('task_progress',id,status='pending_validation',body=node(id)['body'])
    change('validation_record','N11',check='technical',result='UNKNOWN',body='保留周期和真实退订验证未完成；不能通过。',source=source('privacy.md'),files=[source('privacy.md')])
    change('task_progress','N16',status='deferred',body=node('N16')['body'])
    # Intentional real source revision AFTER registration, to demonstrate freshness.
    (root/'privacy.md').write_text('# 隐私与退订 · 版本 2\n\n新增问题：退订后是否保留审计记录，保留多久？\n仍待核定；不能复用版本 1 的判断。合成材料。\n',encoding='utf-8')
    result=store.call('current_frontier',{})
    (root/'DEMO-NOTICE.md').write_text('# 合成演示\n\n16 个任务、3 个阶段、4 条证据、2 项决定、2 份草稿和 1 个待人工门禁。\n只有前三个样例文件的存在与字段检查被标记完成；没有真实业务实现、聊天运行或用户验收。\n',encoding='utf-8')
    return {'root':str(root),'task_count':16,'frontier':result,'gate':node('G01')['status'],'synthetic':True}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root');a=p.parse_args();print(json.dumps(create(a.root),ensure_ascii=False,indent=2))
