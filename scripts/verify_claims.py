import json,collections,re,sys
from datetime import datetime
D='data/data/'
A=json.load(open(D+'accepted_messages.json'));S=json.load(open(D+'spans.json'))
L=json.load(open(D+'logs.json'));DP=json.load(open(D+'deploys.json'))
def t(x): return datetime.fromisoformat(x.replace('Z','+00:00'))
by=collections.defaultdict(list)
for s in S: by[s['attributes']['correlation_id']].append(s)
ck=[]
def c(label,got,want): ck.append((label,got,want,got==want))

c('accepted',len(A),41); c('spans',len(S),273); c('logs',len(L),2820); c('deploys',len(DP),4)
mt=collections.Counter(a['message_type'] for a in A)
c('email/sms/push',(mt['email'],mt['sms'],mt['push']),(29,8,4))
n=collections.Counter(s['name'] for s in S)
c('publish comms-topic',n['publish comms-topic'],41)
c('consume comms-queue',n['consume comms-queue'],37)
c('route push + publish push-queue',n['route push']+n['publish push-queue'],0)
push=[a['correlation_id'] for a in A if a['message_type']=='push']
c('push ids',set(push),{'corr-0005','corr-0010','corr-0020','corr-0036'})
c('push spans in ingest',sum(1 for cid in push for s in by[cid] if s['service']=='comms-ingest'),8)
c('push spans downstream',sum(1 for cid in push for s in by[cid] if s['service']!='comms-ingest'),0)
c('push tenants',collections.Counter(a['tenant_id'] for a in A if a['message_type']=='push'),
  collections.Counter({'org-4471':2,'org-1042':1,'org-6614':1}))
c('push dates',sorted({a['accepted_at'][:10] for a in A if a['message_type']=='push'}),
  ['2026-03-02','2026-03-03','2026-03-05','2026-03-10'])
c('push all 17:52',{a['accepted_at'][11:16] for a in A if a['message_type']=='push'},{'17:52'})
c('log Published type=push',sum(1 for l in L if l['message']=='Published to topic type=push'),4)
c('log Routing type=push',sum(1 for l in L if l['message']=='Routing message type=push'),0)
c('sms provider',{s['attributes']['provider'] for s in S if s['name']=='send sms'},{'aws-pinpoint'})

dups=sorted(cd for cd,v in by.items() if sum(1 for s in v if s['name'].startswith('send '))>1)
c('dup ids',dups,['corr-0014','corr-0022','corr-0035'])
for cd in dups:
    v=by[cd]
    c(f'{cd} publish/consume/send',(sum(1 for s in v if s['name']=='publish email-queue'),
      sum(1 for s in v if s['name']=='consume email-queue'),sum(1 for s in v if s['name']=='send email')),(1,2,2))
    c(f'{cd} same parent',len({s['parent_span_id'] for s in v if s['name']=='consume email-queue'}),1)
    cons=sorted((s for s in v if s['name']=='consume email-queue'),key=lambda x:x['start_time'])
    c(f'{cd} delta s',(t(cons[1]['start_time'])-t(cons[0]['start_time'])).total_seconds(),31.0)
    snd=sorted((s for s in v if s['name']=='send email'),key=lambda x:x['start_time'])
    c(f'{cd} first send 240/202',(snd[0]['duration_ms'],snd[0]['attributes']['provider.status_code']),(240,202))
    c(f'{cd} 2nd send rc=2',snd[1]['attributes'].get('sqs.receive_count'),2)
    c(f'{cd} 2nd consume has NO rc',cons[1]['attributes'].get('sqs.receive_count'),None)
c('dup tenants org-5502',sum(1 for a in A if a['correlation_id'] in dups and a['tenant_id']=='org-5502'),2)
c('org-5502 emails',sum(1 for a in A if a['tenant_id']=='org-5502' and a['message_type']=='email'),5)
c('redelivery log lines',sum(1 for l in L if l['message']=='Received message from queue'),3)

es=[s for s in S if s['name']=='send email']
base=collections.Counter(s['duration_ms'] for s in es if s['attributes']['provider.status_code']==202)
c('email baseline range',(min(base),max(base)),(233,240)); c('modal baseline',base.most_common(1)[0],(235,20))
bad=sorted((s for s in es if s['attributes']['provider.status_code']!=202),key=lambda x:x['start_time'])
c('429 count',len(bad),6)
c('429 ids',[s['attributes']['correlation_id'] for s in bad],['corr-0026','corr-0031','corr-0027','corr-0029','corr-0030','corr-0032'])
c('429 all 4120/3/202',{(s['duration_ms'],s['attributes']['retry_count'],s['attributes']['provider.final_status_code']) for s in bad},{(4120,3,202)})
c("429 distinct tenants",len({s["attributes"]["tenant_id"] for s in bad}),5)
c('0309 emails all slow',[ (s['attributes']['correlation_id'],s['duration_ms']) for s in es if s['start_time'][:10]=='2026-03-09'],
  [('corr-0026',4120),('corr-0031',4120),('corr-0027',4120),('corr-0029',4120),('corr-0030',4120)])
c('onset',bad[0]['start_time'],'2026-03-09T09:00:00.755Z'); c('last slow',bad[-1]['start_time'],'2026-03-10T09:00:00.755Z')
dep={d['sha']:d for d in DP}
c('c52a0f9 gap h',round((t(dep['c52a0f9']['deployed_at'])-t('2026-03-09T09:00:00Z')).total_seconds()/3600,2),5.0)
c('e18d773 at',dep['e18d773']['deployed_at'],'2026-03-10T10:00:00.000Z')
first_clean=[s for s in es if s['start_time']>'2026-03-10T09:00' and s['attributes']['provider.status_code']==202][0]
c('recovered',(first_clean['attributes']['correlation_id'],first_clean['duration_ms'],first_clean['start_time'][:16]),('corr-0033',235,'2026-03-10T11:13'))
c('WARN 429 logs',sum(1 for l in L if l['message'].startswith('Provider returned 429')),6)
e2e={}
for cd,v in by.items():
    acc=[s for s in v if s['name']=='POST /api/v1/messages'][0]
    sn=sorted((s for s in v if s['name'].startswith('send ')),key=lambda x:x['start_time'])
    if sn: e2e[cd]=round((t(sn[0]['start_time'])-t(acc['start_time'])).total_seconds()*1000+sn[0]['duration_ms'])
c('e2e email healthy',e2e['corr-0001'],990); c('e2e throttled',e2e['corr-0026'],4875)
c('e2e ratio',round(4875/990,1),4.9)

sms=[a['correlation_id'] for a in A if a['message_type']=='sms']
c('sms split 8/8',sum(1 for cd in sms if len({s['trace_id'] for s in by[cd]})==2),8)
c('email split 0/29',sum(1 for a in A if a['message_type']=='email' and len({s['trace_id'] for s in by[a['correlation_id']]})>1),0)
c('root spans',collections.Counter((s['service'],s['name']) for s in S if s['parent_span_id'] is None),
  collections.Counter({('comms-ingest','POST /api/v1/messages'):41,('comms-sender','consume sms-queue'):8}))
sender_tr={s['trace_id'] for cd in sms for s in by[cd] if s['service']=='comms-sender'}
c('orphan sms trace ids in logs',sum(1 for l in L if l['trace_id'] in sender_tr),0)
scoped=[l for l in L if l['trace_id']]
c('sms scoped by service',collections.Counter(l['service'] for l in scoped if l['attributes'].get('correlation_id') in set(sms)),
  collections.Counter({'comms-ingest':8,'comms-orchestrator':8}))

c('non-OK spans',sum(1 for s in S if s['status']!='OK'),0)
c('ERROR logs',sum(1 for l in L if l['level']=='ERROR'),0)
c('retry sum',sum(s['attributes'].get('retry_count',0) for s in S),18)
c('provider calls',sum(1 for s in S if s['name'].startswith('send ')),40)
c('queue depth logs',sum(1 for l in L if l['message']=='queue depth metric recorded depth=0'),1200)
c('depth per service',set(collections.Counter(l['service'] for l in L if 'queue depth' in l['message']).values()),{400})
c('depth attrs empty & trace null',all(l['attributes']=={} and l['trace_id'] is None for l in L if 'queue depth' in l['message']),True)
c('health logs',sum(1 for l in L if l['message']=='GET /health 200'),1200)
c('poll logs',sum(1 for l in L if l['message'].startswith('Polling queue')),300)
c('scoped logs',len(scoped),120)
c('unjoinable',len(L)-len(scoped),2700)
c('noise pct',round(2700/2820*100,1),95.7); c('scoped pct',round(120/2820*100,1),4.3)
c('42.6 pct',round(1200/2820*100,1),42.6); c('10.6 pct',round(300/2820*100,1),10.6)
c('DEBUG count',sum(1 for l in L if l['level']=='DEBUG'),1500)

delivered=[cd for cd,v in by.items() if any(s['name'].startswith('send ') for s in v)]
c('delivered once',len(delivered)-len(dups),34); c('dup delivered',len(dups),3)
c('never delivered',41-len(delivered),4)
c('82.9',round(34/41*100,1),82.9); c('7.3',round(3/41*100,1),7.3); c('9.8',round(4/41*100,1),9.8)

c('spans w parent (PARENT_CHILD)',sum(1 for s in S if s['parent_span_id']),224)
ids={s['span_id'] for s in S}
c('dangling parents',sum(1 for s in S if s['parent_span_id'] and s['parent_span_id'] not in ids),0)
c('no dup ledger ids',len({a['correlation_id'] for a in A}),41)
c('accepted_at==ACCEPT.start',all(a['accepted_at']==[s for s in by[a['correlation_id']] if s['name']=='POST /api/v1/messages'][0]['start_time'] for a in A),True)
sp=collections.defaultdict(set)
for s in S: sp[s['attributes']['correlation_id']].add(s['trace_id'])
c('log trace_id contradictions',sum(1 for l in L if l['attributes'].get('correlation_id') and l['trace_id'] and l['trace_id'] not in sp[l['attributes']['correlation_id']]),0)

# hop timings from a clean email trace
v={s['name']:s for s in by['corr-0001']}
def gap(a,b): return round((t(v[b]['start_time'])-(t(v[a]['start_time'])+__import__('datetime').timedelta(milliseconds=v[a]['duration_ms']))).total_seconds()*1000,1)
c('ACCEPT->PUBLISH_TOPIC',gap('POST /api/v1/messages','publish comms-topic'),-26.0)
c('PUBLISH_TOPIC->CONSUME',gap('publish comms-topic','consume comms-queue'),269.0)
c('CONSUME->ROUTE',gap('consume comms-queue','route email'),-18.0)
c('ROUTE->PUBLISH_QUEUE',gap('route email','publish email-queue'),2.0)
c('PUBLISH_QUEUE->CONSUME_QUEUE',gap('publish email-queue','consume email-queue'),379.0)
c('CONSUME_QUEUE->SEND',gap('consume email-queue','send email'),4.0)
topic=set();queue=set()
for cd,vv in by.items():
    d={s['name']:s for s in vv if not s['attributes'].get('sqs.receive_count')}
    if 'consume comms-queue' in d: topic.add(gap.__wrapped__ if 0 else round((t(d['consume comms-queue']['start_time'])-(t(d['publish comms-topic']['start_time'])+__import__('datetime').timedelta(milliseconds=d['publish comms-topic']['duration_ms']))).total_seconds()*1000,1))
c('topic hop zero variance',topic,{269.0})
c('topic hop n',n['consume comms-queue'],37)
c('accepted per day',sorted(collections.Counter(a['accepted_at'][:10] for a in A).items()),
  [('2026-03-02',5),('2026-03-03',5),('2026-03-04',5),('2026-03-05',5),('2026-03-06',5),('2026-03-09',6),('2026-03-10',5),('2026-03-11',5)])
c('0307 0308 weekend',[datetime(2026,3,d).strftime('%A') for d in (7,8)],['Saturday','Sunday'])
c('sending email logs',sum(1 for l in L if l['message'].startswith('Sending email')),29)
attrs=set()
for s in S: attrs|=set(s['attributes'])
c('attribute inventory',attrs,{'correlation_id','message_type','tenant_id','messaging.system','http.status_code','provider','provider.status_code','provider.final_status_code','retry_count','sqs.receive_count'})
c('span kinds',len({s['kind'] for s in S}),5)
c('tenants across channels',len({a['tenant_id'] for a in A}),6)

fail=[x for x in ck if not x[3]]
for lab,got,want,ok in ck: print(('OK  ' if ok else 'FAIL'),lab,'' if ok else f'got={got!r} want={want!r}')
print(f'\n{len(ck)-len(fail)}/{len(ck)} checks passed')
sys.exit(1 if fail else 0)
