"""Read-only pulse dashboard (BTC/ETH hourly + 15m directional lanes)."""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="color-scheme" content="dark"/>
<title>Bot 1 · Directional Lanes</title>
<style>
:root{
  --bg:#12141a;--bg2:#181b24;--card:#1c2029;--line:#2a3040;
  --text:#f0f4f8;--text2:#aeb8c8;--text3:#8f9aad;
  --green:#4ade80;--yellow:#facc15;--red:#f87171;--accent:#a8c8f0;
  --radius:12px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:26px/1.45 "Segoe UI",system-ui,sans-serif}
header{
  padding:14px 18px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  border-bottom:1px solid var(--line);background:var(--bg2);
}
h1{font-size:34px;font-weight:600;margin:0}
.tag{font-size:22px;padding:5px 14px;border-radius:16px;background:var(--card);color:var(--text2)}
.tag.live{color:var(--green)}.tag.warn{color:var(--yellow)}.tag.off{color:var(--red)}
main{max-width:min(1680px,100%);margin:0 auto;padding:14px 20px 24px}
.cap-bar{
  display:flex;flex-wrap:wrap;align-items:baseline;gap:10px 24px;
  background:linear-gradient(135deg,var(--card) 0%,#222836 100%);
  border:1px solid var(--line);border-radius:var(--radius);
  padding:16px 20px;margin-bottom:12px;
}
.cap-main{font-size:40px;font-weight:700;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.cap-label{font-size:22px;color:var(--text2);margin-top:2px}
.cap-sub{font-size:22px;color:var(--text2)}
.cap-sub b{color:var(--text);font-weight:600}
.cap-sub .up{color:var(--green)}.cap-sub .dn{color:var(--red)}
.stats-bar{
  display:flex;flex-wrap:wrap;gap:12px 24px;margin-bottom:12px;
  padding:12px 18px;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
}
.stat{font-size:24px;color:var(--text2)}
.stat b{color:var(--text);font-weight:700;font-variant-numeric:tabular-nums}
.stat .w{color:var(--green)}.stat .l{color:var(--red)}.stat .o{color:var(--yellow)}
.verdict{
  display:flex;align-items:center;gap:8px;font-size:26px;font-weight:600;
  padding:10px 16px;border-radius:var(--radius);background:var(--card);border:1px solid var(--line);
  margin-bottom:12px;
}
.lane-panels{display:flex;flex-direction:column;gap:10px;margin-bottom:16px}
.lane-panel{
  display:grid;grid-template-columns:26px 1fr auto;gap:8px;align-items:center;
  padding:12px 14px;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  cursor:pointer;transition:border-color .12s;
}
.lane-panel:hover{border-color:var(--accent)}
.lane-panel.collapsed .lane-body{display:none}
.lane-panel.lane-btc{border-left:4px solid #fbbf24}
.lane-panel.lane-eth{border-left:4px solid #818cf8}
.lane-panel.lane-btc-15m{border-left:4px solid #f59e0b}
.lane-panel.lane-eth-15m{border-left:4px solid #6366f1}
.lane-head{font-size:24px;font-weight:600;color:var(--text)}
.lane-sub{font-size:20px;color:var(--text2);margin-top:2px}
.lane-meta{font-size:22px;color:var(--text2);text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.lane-body{grid-column:1/-1;padding-top:8px;margin-top:4px;border-top:1px solid var(--line)}
.lane-chev{color:var(--accent);font-size:18px;margin-left:8px}
.trades-empty{color:var(--text2);font-size:22px;padding:8px 4px}
.lane-trade-list{padding-top:4px;max-height:min(60vh,900px);overflow-y:auto}
.trade-line{
  display:flex;justify-content:space-between;align-items:center;gap:10px;
  padding:6px 0;font-size:22px;line-height:1.35;
  border-bottom:1px solid rgba(42,48,64,.45);
}
.trade-line:last-child{border-bottom:0}
.trade-info{min-width:0;color:var(--text2)}
.trade-side{font-weight:600;color:var(--accent)}
.trade-tag{font-size:19px;color:var(--text2);margin-left:4px}
.trade-tag.dir{color:#c4b5fd;font-weight:600}
.trade-tag.dir15{color:#fcd34d;font-weight:600}
.trade-tag.win{color:var(--green)}.trade-tag.loss{color:var(--red)}.trade-tag.open{color:var(--yellow)}
.trade-pnl{font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap;font-size:22px}
.trade-pnl.up{color:var(--green)}.trade-pnl.dn{color:var(--red)}.trade-pnl.neu{color:var(--text2)}
.tl-grid{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(456px,1fr));gap:8px;min-width:0;
}
.tl-row{
  display:grid;grid-template-columns:26px 1fr auto;gap:8px;align-items:center;
  padding:8px 12px;background:var(--card);border:1px solid var(--line);border-radius:8px;
}
.tl-dot{width:17px;height:17px;border-radius:50%;flex-shrink:0}
.tl-green{background:var(--green);box-shadow:0 0 6px rgba(74,222,128,.55)}
.tl-yellow{background:var(--yellow);box-shadow:0 0 6px rgba(250,204,21,.45)}
.tl-red{background:var(--red);box-shadow:0 0 6px rgba(248,113,113,.55)}
.tl-name{font-size:24px;color:var(--text)}
.tl-val{font-size:22px;color:var(--text2);text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.tl-hint{
  grid-column:1/-1;font-size:20px;color:var(--text2);
  padding-top:6px;margin-top:2px;border-top:1px solid var(--line);
}
.tl-exp{cursor:pointer;transition:border-color .12s}
.tl-exp:hover{border-color:var(--accent)}
.tl-exp.collapsed .tl-hint{display:none}
.tl-chev{color:var(--accent);font-size:18px;margin-left:8px}
.tl-section{
  grid-column:1/-1;font-size:20px;font-weight:600;color:var(--accent);
  text-transform:uppercase;letter-spacing:.06em;padding:8px 2px 4px;
}
.decision-panel{
  margin-bottom:14px;padding:12px 16px;background:var(--card);
  border:1px solid var(--line);border-radius:var(--radius);
}
.decision-head{font-size:24px;font-weight:600;color:var(--accent);margin-bottom:8px}
.decision-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:10px}
.decision-block{padding:10px 12px;background:var(--bg2);border:1px solid var(--line);border-radius:8px}
.decision-block h3{font-size:20px;font-weight:600;margin:0 0 6px;color:var(--text)}
.decision-line{font-size:20px;color:var(--text2);line-height:1.4;padding:3px 0}
.decision-line b{color:var(--text)}
.decision-line .ok{color:var(--green)}.decision-line .bad{color:var(--red)}.decision-line .warn{color:var(--yellow)}
.cand-list{max-height:min(42vh,520px);overflow-y:auto;margin-top:4px}
.cand-line{
  padding:6px 0;font-size:19px;line-height:1.35;color:var(--text2);
  border-bottom:1px solid rgba(42,48,64,.45);
}
.cand-line:last-child{border-bottom:0}
.cand-mkt{color:var(--text);font-weight:600}
.cand-reason{color:var(--accent)}
.foot{margin-top:14px;color:var(--text2);font-size:20px}
@media(max-width:420px){.tl-grid{grid-template-columns:1fr}.cap-main{font-size:34px}.decision-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <h1>Bot 1 · Directional</h1>
  <span class="tag">BTC/ETH 1h + 15m</span>
  <span class="tag" id="health">Loading…</span>
  <span class="tag" id="meta" style="color:var(--text3)"></span>
</header>
<main>
  <div class="cap-bar" id="cap-bar"></div>
  <div class="stats-bar" id="stats-bar"></div>
  <div class="verdict" id="verdict"></div>
  <section class="lane-panels" id="lane-panels" aria-label="Recent trades by lane"></section>
  <div class="tl-grid" id="tl-grid"></div>
  <div class="foot">Updates every minute · tier engine + lane learners · paper only · no live trading</div>
</main>
<script>
const TRADE_LIMIT=50;
const $=(h)=>{const t=document.createElement('template');t.innerHTML=h.trim();return t.content.firstChild};
const f=(x,d=2)=>x==null||x===''?'—':(typeof x==='number'?x.toFixed(d):String(x));
const usd=(x)=>x==null?'—':'$'+Number(x).toFixed(2);
const pct=(x)=>x==null?'—':(x>=0?'+':'')+Number(x).toFixed(2)+'%';
const dot=(c)=>'<span class="tl-dot tl-'+c+'"></span>';
const LANE_TRADE_ORDER=[
  {key:'btc_1h',name:'BTC 1h · last 50',cls:'lane-btc'},
  {key:'btc_15m',name:'BTC 15m · last 50',cls:'lane-btc-15m'},
  {key:'eth_1h',name:'ETH 1h · last 50',cls:'lane-eth'},
  {key:'eth_15m',name:'ETH 15m · last 50',cls:'lane-eth-15m'},
];
const lanePanelOpen={btc_1h:false,btc_15m:false,eth_1h:false,eth_15m:false};

function fmtTsShort(ts){
  if(ts==null)return '—';
  try{
    const d=new Date(Number(ts)*1000);
    return d.toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
  }catch(e){return '—';}
}
function fmtAge(sec){
  if(sec==null)return '—';
  const s=Math.round(Number(sec));
  if(!Number.isFinite(s)||s<0)return '—';
  if(s<60)return s+' seconds ago';
  if(s<3600)return Math.floor(s/60)+' min ago';
  return Math.floor(s/3600)+' hr ago';
}
function tradeOutcome(x){
  const st=(x.status||'').toLowerCase();
  if(st==='open')return {label:'still open',cls:'open',pnlCls:'neu',pnl:'—'};
  if(x.won===true)return {label:'won',cls:'win',pnlCls:'up',pnl:usd(x.pnl_usd)};
  if(x.won===false)return {label:'lost',cls:'loss',pnlCls:'dn',pnl:usd(x.pnl_usd)};
  return {label:st||'—',cls:'',pnlCls:'neu',pnl:x.pnl_usd==null?'—':usd(x.pnl_usd)};
}
function laneBadge(x){
  const tf=(x.market_tf||((x.research||{}).market_series)||'').toLowerCase();
  if(tf==='15m'||String(tf).includes('15m'))return {txt:'15M',cls:'dir15'};
  return {txt:'1H',cls:'dir'};
}
function buildTradeRowsHtml(positions){
  const pos=(positions||[]).slice(0,TRADE_LIMIT);
  if(!pos.length)return '<div class="trades-empty">No trades yet in this lane.</div>';
  return pos.map(x=>{
    const r=x.research||{};
    const oc=tradeOutcome(x);
    const lb=laneBadge(x);
    const sym=x.trade_symbol||'—';
    const mtf=x.market_tf||r.market_series||'—';
    const tvTf=x.tv_timeframe?(' · TV '+x.tv_timeframe+'m'):'';
    const kind=x.market_kind_label?(' · '+x.market_kind_label):'';
    const strike=x.strike_price!=null?(' · strike '+f(x.strike_price,0)):'';
    const winEntry=x.window_entry_label?(' · '+x.window_entry_label):'';
    const ttm=x.ttm_label?(' · TTM '+x.ttm_label):'';
    const settled=x.outcome_settled?' · final result known':'';
    return '<div class="trade-line">'
      +'<div class="trade-info"><span class="trade-tag '+lb.cls+'">'+lb.txt+'</span>'
      +'<span class="trade-side">'+(x.side||'—')+'</span>'
      +'<span class="trade-tag '+oc.cls+'">'+oc.label+'</span>'
      +'<span class="trade-tag"> '+sym+' · '+mtf+' market'+kind+strike+tvTf+' · entry '+f(x.entry_price,3)+settled+'</span>'
      +'<br><span class="trade-tag">'+fmtTsShort(x.entry_ts)+winEntry+ttm+'</span></div>'
      +'<div class="trade-pnl '+oc.pnlCls+'">'+oc.pnl+'</div></div>';
  }).join('');
}
function laneTradeLight(st){
  if(!st||(st.total||0)===0)return 'yellow';
  if((st.settled||0)>0&&(st.win_rate==null||(st.losses||0)>(st.wins||0)))return (st.losses||0)>(st.wins||0)?'red':'yellow';
  if((st.wins||0)>(st.losses||0))return 'green';
  if((st.settled||0)>0)return 'yellow';
  return 'green';
}
function renderLanePanels(container,laneTrades,lanes,lanePnl){
  container.innerHTML='';
  LANE_TRADE_ORDER.forEach(lane=>{
    const trades=((laneTrades||{})[lane.key])||[];
    const st=((lanes||{})[lane.key])||{};
    const open=!!lanePanelOpen[lane.key];
    const el=$('<div class="lane-panel '+lane.cls+(open?'':' collapsed')+'"></div>');
    const wr=st.win_rate!=null?(st.win_rate*100).toFixed(0)+'%':'—';
    const shown=Math.min(trades.length,TRADE_LIMIT);
    const pnlv=(lanePnl||{})[lane.key];
    const pnlCls=(pnlv==null)?'':(pnlv>=0?'up':'dn');
    const pnlTxt=pnlv!=null?usd(pnlv):'—';
    let html=dot(laneTradeLight(st))
      +'<div><div class="lane-head">'+lane.name+'<span class="lane-chev">'+(open?'▾':'▸')+'</span></div>'
      +'<div class="lane-sub">Last '+TRADE_LIMIT+' trades · tap to '+(open?'hide':'show')+'</div></div>'
      +'<div class="lane-meta">'+shown+' listed · '+f(st.settled,0)+' done · '
      +f(st.wins,0)+'W/'+f(st.losses,0)+'L · '+wr
      +'<br><b class="'+pnlCls+'">'+pnlTxt+'</b></div>'
      +'<div class="lane-body"><div class="lane-trade-list">'+buildTradeRowsHtml(trades)+'</div></div>';
    el.innerHTML=html;
    el.addEventListener('click',()=>{
      lanePanelOpen[lane.key]=!lanePanelOpen[lane.key];
      renderLanePanels(container,laneTrades,lanes,lanePnl);
    });
    container.appendChild(el);
  });
}
function renderStats(el,lanes,cap,s){
  lanes=lanes||{};
  const btc1=lanes.btc_1h||{};
  const btc15=lanes.btc_15m||{};
  const eth1=lanes.eth_1h||{};
  const eth15=lanes.eth_15m||{};
  const all=lanes.directional||{};
  function laneStat(label,st){
    const wr=st.win_rate!=null?(st.win_rate*100).toFixed(0)+'%':'—';
    const pnl=st.realized_pnl_usd;
    const pnlCls=(pnl==null)?'':(pnl>=0?'up':'dn');
    return '<span class="stat"><b>'+label+'</b> '
      +f(st.settled,0)+' done · '+f(st.wins,0)+'W/'+f(st.losses,0)+'L · '+wr
      +(pnl!=null?' · <b class="'+pnlCls+'">'+usd(pnl)+'</b>':'')
      +' · '+f(st.open,0)+' open</span>';
  }
  const ol=s.osmani_loop||{};
  const disc=((ol.lanes||{}).discovery)||{};
  const osmaniTag=ol.enabled?('Osmani ON · scan '+f(disc.interval_s||60,0)+'s'):'tier tick path';
  el.innerHTML=laneStat('BTC 1h',btc1)+laneStat('BTC 15m',btc15)
    +laneStat('ETH 1h',eth1)+laneStat('ETH 15m',eth15)
    +'<span class="stat">Combined <b>'+f(all.settled,0)+'</b> settled · '
    +usd(all.realized_pnl_usd!=null?all.realized_pnl_usd:cap.realized_pnl_usd)+'</span>'
    +'<span class="stat">'+osmaniTag+'</span>';
}
function addRow(rows,name,val,hint,light,expandable){rows.push({name,val,hint,light,expandable});}
function addSection(rows,title){rows.push({section:title});}

function captureLight(ratio){
  if(ratio==null||ratio==='')return 'yellow';
  const r=Number(ratio);
  if(r<0)return 'red';
  if(r>=0.4)return 'green';
  if(r>=0.1)return 'yellow';
  return 'red';
}

function fmtBandSeconds(minS,maxS){
  if(minS==null&&maxS==null)return '—';
  const lo=Math.round(Number(minS||0)/60);
  const hi=Math.round(Number(maxS||0)/60);
  return lo+'–'+hi+'m into hour';
}

function buildRows(s){
  const rows=[];
  const loops=s.loops||{};
  const cap=s.capital||{};
  const led=s.ledger||{};
  const statusAge=(Date.now()/1000)-(Number(s.ts)||0);
  const lanes=s.lane_stats||{};
  const dr=s.directional_risk||{};
  const arch=s.series_architecture||{};
  const ol=s.osmani_loop||{};
  const he=s.learned_hourly_entry_gate||{};
  const pt=s.pre_trade_analysis||{};
  const sz=s.sizing||{};
  const dirLoop=((loops.loops||{}).directional)||{};
  const dirStatus=dirLoop.status||{};

  addSection(rows,'Osmani loop — trade authority');
  if(ol.enabled){
    const disc=((ol.lanes||{}).discovery)||{};
    const exe=((ol.lanes||{}).execution)||{};
    const mc=ol.maker_checker||{};
    const ev=mc.evaluator||{};
    const cb=ol.circuit_breaker||{};
    const skill=ol.asset_triage_skill||{};
    addRow(rows,'Discovery lane',
      f(disc.scans,0)+' scans · '+f(disc.emitted,0)+' emitted · every '+f(disc.interval_s||60,0)+'s',
      'triage rejected '+f(skill.rejected||disc.triage_rejected,0)+' · skill polymarket_asset_triage',
      cb.tripped?'red':'green');
    addRow(rows,'Execution lane',
      f(exe.executed,0)+' filled / '+f(exe.processed,0)+' processed',
      'rejected '+f(exe.rejected,0)+' · independent book verify before fill',
      (exe.executed||0)>0?'green':'yellow');
    addRow(rows,'Maker-checker',
      f(ev.verified_ok,0)+' verified · '+f(ev.verified_reject,0)+' rejected',
      'assumed-failed until verified · generator TradeGenerator',
      (ev.verified_ok||0)>(ev.verified_reject||0)?'green':'yellow');
    if(cb.enabled){
      addRow(rows,'Circuit breaker',
        cb.tripped?'TRIPPED':'OK',
        'spent today '+usd(cb.spent_today_usd)+' · API calls '+f(cb.api_calls_today,0),
        cb.tripped?'red':'green');
    }
  }else{
    addRow(rows,'Osmani loop','OFF',
      'legacy tick path · authority '+f(dirStatus.authority,'—'),
      'yellow');
  }

  addSection(rows,'BTC / ETH hourly — scorecard');
  function laneRow(name,settled,wins,losses,wr,pnlv,open,note){
    const parts=[f(settled,0)+' settled'];
    if(wins!=null||losses!=null)parts.push(f(wins,0)+'W / '+f(losses,0)+'L');
    if(wr!=null)parts.push((wr*100).toFixed(0)+'% win');
    const light=((settled||0)===0)?'yellow':(pnlv==null?'yellow':(pnlv>=0?'green':'red'));
    addRow(rows,name,parts.join(' · '),
      'P&amp;L '+usd(pnlv)+' · '+f(open,0)+' open'+(note?' · '+note:''),light);
  }
  const btc1=lanes.btc_1h||{};
  const eth1=lanes.eth_1h||{};
  const btc15=lanes.btc_15m||{};
  const eth15=lanes.eth_15m||{};
  laneRow('BTC 1h up/down',btc1.settled,btc1.wins,btc1.losses,btc1.win_rate,
    btc1.realized_pnl_usd,btc1.open,'hourly feed · GateAutoTuner');
  laneRow('ETH 1h up/down',eth1.settled,eth1.wins,eth1.losses,eth1.win_rate,
    eth1.realized_pnl_usd,eth1.open,'hourly feed · GateAutoTuner');
  laneRow('BTC 15m up/down',btc15.settled,btc15.wins,btc15.losses,btc15.win_rate,
    btc15.realized_pnl_usd,btc15.open,'15m feed · lane learner');
  laneRow('ETH 15m up/down',eth15.settled,eth15.wins,eth15.losses,eth15.win_rate,
    eth15.realized_pnl_usd,eth15.open,'15m feed · lane learner');
  if(cap.total_realized_pnl_usd!=null){
    addRow(rows,'Combined directional',
      f((lanes.directional||{}).settled||0,0)+' settled',
      'Total realized P&amp;L '+usd(cap.total_realized_pnl_usd),
      cap.total_realized_pnl_usd>=0?'green':'yellow');
  }

  addSection(rows,'15m lane · self-tuning WR');
  const ln=s.lane_15m_learner||{};
  const pol=ln.policy||{};
  const lnRoll=ln.rolling||{};
  if(ln.enabled!==false){
    const sweet=(pol.sweet||[pol.sweet_min,pol.sweet_max]).join('–');
    const ttc=(pol.prefer_ttc||[pol.prefer_ttc_min,pol.prefer_ttc_max]).join('–');
    addRow(rows,'15m strategy policy',
      (pol.side_mode||'both')+' · edge ≥ '+f(pol.min_edge,3),
      'SSO '+f(pol.min_sso,0)+'–'+f(pol.max_sso,0)+'s · TTC '+ttc+'s · sweet '+sweet
      +' · probe '+(pol.probe_enabled?'ON':'OFF'),
      (lnRoll.win_rate!=null&&lnRoll.win_rate>=0.6)?'green':'yellow');
    addRow(rows,'15m learner evidence',
      f(lnRoll.n,0)+' fills · WR '+(lnRoll.win_rate!=null?(lnRoll.win_rate*100).toFixed(0)+'%':'—'),
      'target '+f((ln.targets||{}).target_wr*100,0)+'% · last '+(ln.last_action||'hold')
      +' · '+f(lnRoll.fills_per_hour,1)+'/hr',
      ln.last_action==='tighten'?'yellow':(ln.last_action==='loosen'?'green':'yellow'));
  }else{
    addRow(rows,'15m lane learner','OFF','enable PULSE_LANE_15M_LEARN_ENABLED','yellow');
  }

  addSection(rows,'Cross-horizon · 15m↔1h shared policy');
  const xh=s.cross_horizon_learner||{};
  const xhp=xh.policy||{};
  const xh15=(xh.rolling||{})['15m']||{};
  const xh1h=(xh.rolling||{})['1h']||{};
  if(xh.enabled!==false){
    addRow(rows,'Shared overlays',
      '1h SSO≥'+f(xhp.h1_min_sso_frac,2)
      +' · prefer_down '+(xhp.h1_prefer_down?'ON':'OFF')
      +' · block_early_up '+(xhp.h1_block_early_up?'1h':'')+(xhp.m15_block_early_up?'/15m':''),
      'restrict/size only · gate authoritative · locked',
      'green');
    addRow(rows,'Cross-horizon evidence',
      '15m n='+f(xh15.n,0)+' WR '+(xh15.win_rate!=null?(xh15.win_rate*100).toFixed(0)+'%':'—')
      +' · 1h n='+f(xh1h.n,0)+' WR '+(xh1h.win_rate!=null?(xh1h.win_rate*100).toFixed(0)+'%':'—'),
      'last '+(xh.last_action||'hold')+' · blocked '+f(((xh.counters||{}).blocked)||0,0),
      'yellow');
  }else{
    addRow(rows,'Cross-horizon learner','OFF','enable PULSE_CROSS_HORIZON_LEARN_ENABLED','yellow');
  }

  addSection(rows,'Hourly auto-tune (GateAutoTuner)');
  const gat=s.gate_auto_tune||{};
  const gRoll=gat.rolling||{};
  const hwr=s.high_wr_mode||{};
  if(gat.enabled!==false){
    addRow(rows,'Hourly scalar gates',
      'entry ≥ '+f(hwr.min_entry_price,2)+' · edge ≥ '+f(hwr.min_edge,3),
      'sweet band + hourly SSO nudged on settled 1h fills only',
      'green');
    addRow(rows,'Hourly tuner evidence',
      f(gRoll.n,0)+' fills · WR '+(gRoll.win_rate!=null?(gRoll.win_rate*100).toFixed(0)+'%':'—'),
      'last '+(gat.last_action||'hold')+' · '+f(gRoll.fills_per_hour,1)+'/hr · 15m excluded',
      gat.last_action==='tighten'?'yellow':(gat.last_action==='loosen'?'green':'yellow'));
  }

  addSection(rows,'SAWR — Self-Adjusting Win-Rate');
  const sawr=s.sawr||{};
  const sRoll=sawr.rolling||{};
  if(sawr.enabled!==false){
    addRow(rows,'Fill-Quality Pareto',
      'U='+f(sawr.utility,3)+' · stance '+(sawr.stance||'hold'),
      'Wilson LB '+(sRoll.wilson_lb!=null?(sRoll.wilson_lb*100).toFixed(0)+'%':'—')
      +' · WR '+(sRoll.win_rate!=null?(sRoll.win_rate*100).toFixed(0)+'%':'—')
      +' · n='+f(sRoll.n,0),
      sawr.veto_loosen?'yellow':'green');
    addRow(rows,'Side affinity + arbitration',
      'last '+(sawr.last_action||'hold')+' · η='+f(sawr.step_scale,2)
      +' · regime '+f(sawr.regime_factor,2),
      sawr.veto_loosen?'veto loosen active (kill floor)':'Beta posteriors size/soft-block sides',
      sawr.veto_loosen?'yellow':'green');
  }else{
    addRow(rows,'SAWR','OFF','enable PULSE_SAWR_ENABLED','yellow');
  }

  addSection(rows,'Entry timing · sizing · pre-trade');
  const band=he.target_entry_band_s||[he.min_seconds_since_open,he.max_seconds_since_open];
  addRow(rows,'Hourly entry band',
    fmtBandSeconds(band[0],band[1]),
    (he.enabled?'learned gate ON':'gate OFF')+' · too early '+f(he.too_early,0)
    +' · rejected '+f(he.rejected,0),
    he.enabled?'green':'yellow');
  addRow(rows,'Autonomous bet size',
    sz.osmani_autonomous?('half-Kelly × pre-trade · $'+f(sz.osmani_min_usd,0)+'–$'+f(sz.hard_cap_usd,0)):'fixed $'+f(sz.actual_size_usd,2),
    sz.note||'Osmani path sizes each fill from conviction',
    sz.osmani_autonomous?'green':'yellow');
  if(pt.enabled){
    addRow(rows,'Pre-trade synthesis',
      'min score '+f(pt.min_score,2)+' · accepted '+f(pt.accepted,0)+' / rejected '+f(pt.rejected,0),
      'readiness scales size · restrict-only (never bypasses execution gate)',
      (pt.rejected||0)<(pt.accepted||0)?'green':'yellow');
  }

  addSection(rows,'Markets — scan vs trade');
  const tradeSlugs=(dr.directional_series_slugs||[]).join(', ')||'—';
  const scanN=(arch.scan_slugs||[]).length;
  const mf=s.markets_feed||{};
  const m15=mf.m15||{};
  const hr=mf.hourly||{};
  addRow(rows,'Directional feeds',
    (hr.enabled?'1h ON':'1h off')+' · '+(m15.enabled?'15m ON':'15m off'),
    (m15.assets||[]).join('+')+' 15m · '+f(m15.window_seconds,0)+'s windows',
    (hr.enabled&&m15.enabled)?'green':'yellow');
  addRow(rows,'Trades (directional slugs)',
    tradeSlugs,
    'tier engine tick · '+f(led.trades,0)+' ledger settled',
    dr.directional_enabled?'green':'red');
  addRow(rows,'Scans (arb brain)',
    scanN+' series',
    '5m/15m/hourly crypto — observe + dep-arb; BTC/ETH 1h+15m execute directionally',
    'green');

  // ---- TradingView: intrahour RSI (observe-only; feeds council + pre-trade) ----
  addSection(rows,'TradingView · observe-only (BTC ETH)');
  const tvd=s.tradingview||{};
  const bysym=tvd.tradingview_latest_by_symbol||{};
  const mem2=(s.llm_council||{}).members||{};
  const mtf=mem2['tv_mtf'];
  const mtfTfs=(tvd.tradingview_mtf_timeframes||[]).join(', ')||'5–60m RSI div ladder';
  addRow(rows,'TV config',
    'tracking '+mtfTfs+'m · assets BTC ETH',
    'Hermes Crypto RSI — per-asset council match (trade lanes: BTC + ETH only)',
    'green');
  const histCap=tvd.tradingview_alert_history_per_symbol||50;
  const histCounts=tvd.tradingview_alert_history_counts||{};
  const path15=tvd.tradingview_15m_price_path||{};
  const pathFocus=path15.focus||{};
  const shortT=pathFocus.short_term||{};
  const regimeT=pathFocus.regime||pathFocus;
  const shortPat=((shortT.trend||{}).pattern)||'—';
  const regimePat=((regimeT.trend||{}).pattern)||(pathFocus.trend||{}).pattern||'—';
  const lean=pathFocus.trade_lean||pathFocus.lean||'—';
  const align=pathFocus.alignment||'—';
  addRow(rows,'TV 15m short lean (last '+(path15.short_n||8)+')',
    shortPat+' · lean='+lean+' · '+align+(shortT.n!=null?' · n='+shortT.n:''),
    'Last 6–8 bar-close alerts ≈ current trade trend (drives size bias)',
    shortT.n>0?'green':'yellow');
  addRow(rows,'TV 15m regime path (last '+histCap+')',
    regimePat+(regimeT.n!=null?' · n='+regimeT.n:'')+(regimeT.price_delta_pct!=null?' · Δ'+regimeT.price_delta_pct+'%':''),
    'Last 50 alerts ≈ HTF structure for Grok (context only)',
    regimeT.n>0?'green':'yellow');
  addRow(rows,'TV alert FIFO counts',
    Object.keys(histCounts).length?Object.keys(histCounts).map(k=>k+':'+histCounts[k]).join(' · '):'empty',
    'hard cap '+histCap+' per symbol — oldest dropped',
    Object.keys(histCounts).length?'green':'yellow');
  if(mtf){
    addRow(rows,'TV council voter (MTF)',
      (mtf.stance||'cold').toUpperCase()+(mtf.accuracy!=null?' · '+(mtf.accuracy*100).toFixed(0)+'% acc (n='+f(mtf.n,0)+')':' (learning)'),
      'tv_mtf agreement on fresh ladder alerts — graded follow/fade',
      mtf.stance==='follow'?'green':(mtf.stance==='fade'?'yellow':'yellow'));
  }
  const ladderTfs=(tvd.tradingview_mtf_timeframes||['5','10','15','20','25','30','35','40','45','50','55','60']).map(String);
  const tvMembers=ladderTfs.map(tf=>'tv_'+tf+'m').concat(['tv_2h_trend']).map(k=>mem2[k]).filter(Boolean);
  if(tvMembers.length){
    const best=tvMembers.sort((a,b)=>(b.n||0)-(a.n||0))[0];
    addRow(rows,'TV per-TF graders',
      tvMembers.length+' active · best '+((best.stance||'cold').toUpperCase()),
      'each intrahour TF graded independently on outcomes',
      'green');
  }
  const byTf=tvd.tradingview_latest_by_timeframe||{};
  const symKeys=Object.keys(tvd.tradingview_latest_by_symbol||bysym).sort();
  const maxAge=(s.llm_council||{}).council_tv_max_age_s||3600;
  const nowSec=Date.now()/1000;
  const activeTfs=new Set(ladderTfs);
  const allowedSyms=new Set(['BTCUSD','ETHUSD']);
  function ladderFor(sym){
    return ladderTfs.map(tf=>{
      const snap=byTf[sym+'@'+tf]||{};
      const dir=snap.direction||'—';
      const age=(snap.ts!=null)?(nowSec-Number(snap.ts)):null;
      const fresh=activeTfs.has(tf)&&(age!=null)&&(age<=maxAge);
      const tag=fresh?dir:(dir==='—'?'—':'stale '+dir);
      return tf+'m '+tag;
    }).join(' → ');
  }
  if(symKeys.length){
    symKeys.forEach(sym=>{
      if(!allowedSyms.has(sym))return;
      const ladder=ladderFor(sym);
      const freshCt=ladderTfs.filter(tf=>{
        const snap=byTf[sym+'@'+tf]||{};
        const age=(snap.ts!=null)?(nowSec-Number(snap.ts)):null;
        return activeTfs.has(tf)&&snap.direction&&age!=null&&age<=maxAge;
      }).length;
      const light=freshCt>=3?'green':(freshCt>=1?'yellow':'red');
      addRow(rows,sym+' · MTF ladder',ladder,
        freshCt+'/'+ladderTfs.length+' fresh · council reads this order',
        light,true);
    });
  }else{
    addRow(rows,'TV per-asset signals','No alerts received yet',
      'Set Hermes Crypto RSI on BTC + ETH charts · webhook → VPS','red');
  }
  addRow(rows,'TV alerts landing',
    f(tvd.tradingview_alerts_valid,0)+' valid / '+f(tvd.tradingview_alerts_received,0)+' received',
    'rejected '+f(tvd.tradingview_alerts_rejected,0)+' · observe-only (council grades, never hard-gates)',
    (tvd.tradingview_alerts_valid||0)>0?'green':'yellow');

  const tv2h=tvd.tv_2h_review||{};
  if(tv2h.enabled){
    const f2=tv2h.focus||{};
    const lb=Math.round((tv2h.lookback_s||7200)/3600);
    const dir=(f2.trend_direction||'—').toUpperCase();
    const align=f2.alignment||'—';
    const conf=f2.confidence!=null?(f2.confidence*100).toFixed(0)+'%':'—';
    const delta=f2.price_delta_pct!=null?(f2.price_delta_pct>=0?'+':'')+f2.price_delta_pct.toFixed(2)+'%':'—';
    const councilGrade=tv2h.council_grade_enabled?' · council grades tv_2h_trend':'';
    const pc=f2.phase_counts||{};
    const phases='early '+f(pc.pre_band,0)+' · in-band '+f(pc.in_band,0)+' · late '+f(pc.post_band,0);
    addRow(rows,'TV 2h trend review',
      dir+' · '+f(f2.alert_count,0)+' alerts / '+lb+'h',
      'price '+delta+' · '+align+' · conf '+conf+' · '+phases+councilGrade+' · observe-only',
      f2.aligned?'green':(f2.divergent?'yellow':'yellow'));
  }

  // ---- External signals (advisory; Osmani + execution gate decide fills) ----
  addSection(rows,'Advisory signals');
  const grok=s.grok_decider||{};
  const council=s.llm_council||{};
  addRow(rows,'LLM council',
    council.enabled?('ON · margin '+f(council.min_margin,2)):'off',
    'quant + Grok members grade outcomes · Osmani path uses triage + verify',
    council.enabled?'green':'yellow');
  addRow(rows,'Grok decider',
    (grok.mode||'off')+(grok.enabled===false?' (off)':''),
    'shadow — grades direction, not a solo trade gate',
    grok.mode==='shadow'?'green':'yellow');

  addSection(rows,'Lane detail · tap to expand');
  addRow(rows,'Osmani triage thresholds',
    'sweet '+f(((ol.asset_triage_skill||{}).thresholds||{}).sweet_min,2)
    +'–'+f(((ol.asset_triage_skill||{}).thresholds||{}).sweet_max,2),
    'min depth $'+f(((ol.asset_triage_skill||{}).thresholds||{}).min_depth_usd,0)
    +' · TV observe-only (never hard-gates)',
    ol.enabled?'green':'yellow',true);

  addSection(rows,'Bot health');
  addRow(rows,'Is the bot running?','Heartbeat '+f(s.ticks,0)+' · updated '+fmtAge(statusAge),
    s.paper_only?'Practice mode — safe':'WARNING: check live flag',
    statusAge<45&&s.ticks>5?'green':(statusAge<120?'yellow':'red'));
  addRow(rows,'Background jobs',loops.all_live?'All loops OK':'Some loops stalled',
    f((loops.stalled||[]).length,0)+' need attention'
    +(ol.enabled?' · osmani_discovery + execution live':''),
    (loops.stalled||[]).length===0?'green':'red');
  if(cap.total_realized_pnl_usd!=null){
    addRow(rows,'Whole-bot P&amp;L',usd(cap.total_realized_pnl_usd),
      'directional BTC/ETH hourly only',
      cap.total_realized_pnl_usd>=0?'green':'yellow');
  }

  return rows;
}

function overallLight(s){
  const cap=s.capital||{};
  if(!s.available)return {light:'red',text:'No data — bot may be stopped'};
  if(s.live_trading_enabled)return {light:'red',text:'WARNING: live trading is ON'};
  const ol=s.osmani_loop||{};
  const he=s.learned_hourly_entry_gate||{};
  const band=he.target_entry_band_s||[he.min_seconds_since_open,he.max_seconds_since_open];
  const bandTxt=fmtBandSeconds(band[0],band[1]);
  if(ol.enabled){
    const exe=((ol.lanes||{}).execution)||{};
    if((exe.executed||0)>0)return {light:'green',text:'Osmani + directional lanes active'};
    return {light:'yellow',text:'Scanning BTC/ETH 1h+15m — tier engine deciding entries'};
  }
  const m15on=((s.markets_feed||{}).m15||{}).enabled;
  if(m15on)return {light:'yellow',text:'Tier engine on BTC/ETH 1h+15m — collecting lane evidence'};
  const total=cap.total_realized_pnl_usd;
  if(total!=null){
    if(total>0)return {light:'green',text:'Paper bot net positive — see BTC/ETH cards below'};
    if(total<0)return {light:'yellow',text:'Paper bot net negative — review hourly lanes'};
  }
  const lanes=s.lane_stats||{};
  const traded=(lanes.directional||{}).total;
  if(traded>0)return {light:'green',text:'Directional lanes active'};
  return {light:'yellow',text:'Watching markets — no trades yet'};
}

function renderRows(grid,rows){
  grid.innerHTML='';
  rows.forEach(r=>{
    if(r.section){
      grid.appendChild($('<div class="tl-section">'+r.section+'</div>'));
      return;
    }
    if(r.expandable){
      // click-to-expand lane card: header always visible, details revealed on tap
      const el=$('<div class="tl-row tl-exp collapsed"></div>');
      let html=dot(r.light)+'<span class="tl-name">'+r.name+'<span class="tl-chev">▸</span></span>'
        +'<span class="tl-val">'+r.val+'</span>';
      if(r.hint)html+='<div class="tl-hint">'+r.hint+'</div>';
      el.innerHTML=html;
      el.addEventListener('click',()=>{
        const open=el.classList.toggle('collapsed')===false;
        const c=el.querySelector('.tl-chev');
        if(c)c.textContent=open?'▾':'▸';
      });
      grid.appendChild(el);
      return;
    }
    const el=$('<div class="tl-row"></div>');
    let html=dot(r.light)+'<span class="tl-name">'+r.name+'</span><span class="tl-val">'+r.val+'</span>';
    if(r.hint)html+='<div class="tl-hint">'+r.hint+'</div>';
    el.innerHTML=html;
    grid.appendChild(el);
  });
}

async function fetchJson(url,timeoutMs=20000){
  const ctrl=new AbortController();
  const t=setTimeout(()=>ctrl.abort(),timeoutMs);
  try{
    const r=await fetch(url,{cache:'no-store',signal:ctrl.signal});
    if(!r.ok)throw new Error('HTTP '+r.status);
    return await r.json();
  }finally{clearTimeout(t);}
}

function setTag(id,text,cls){
  const el=document.getElementById(id);
  el.textContent=text;
  el.className='tag'+(cls?' '+cls:'');
}

async function tick(){
  setTag('health','Loading…','');
  let s,l;
  try{
    [s,l]=await Promise.all([
      fetchJson('/api/polymarket/training/btc_pulse'),
      fetchJson('/api/polymarket/training/btc_pulse/ledger?summary=1'),
    ]);
  }catch(e){setTag('health',e&&e.name==='AbortError'?'Timed out':'Cannot reach bot','off');return;}
  if(!s.available){setTag('health','No data','off');return;}
  setTag('health','Connected','live');
  document.getElementById('meta').textContent=
    'Last refresh · '+new Date().toLocaleTimeString();

  const cap=s.capital||{};
  const lanes=(l&&l.lane_stats)||{};
  const btc1Pnl=(lanes.btc_1h||{}).realized_pnl_usd;
  const btc15Pnl=(lanes.btc_15m||{}).realized_pnl_usd;
  const eth1Pnl=(lanes.eth_1h||{}).realized_pnl_usd;
  const eth15Pnl=(lanes.eth_15m||{}).realized_pnl_usd;
  const dirPnl=cap.realized_pnl_usd;
  const totalCap=cap.total_on_hand_usd;
  const startCap=cap.starting_capital_usd;
  const totalRet=cap.total_return_pct;
  const capCls=(totalCap==null)?'':(totalCap>=startCap?'up':'dn');
  const laneTotal=(lanes.directional||{}).total;

  document.getElementById('cap-bar').innerHTML=
    '<div><div class="cap-main '+capCls+'">'+usd(totalCap)+'</div>'
    +'<div class="cap-label">Total paper capital</div>'
    +(startCap!=null?'<div class="cap-label">Started '+usd(startCap)
      +(totalRet!=null?' · '+pct(totalRet)+' overall':'')+'</div>':'')+'</div>'
    +'<div class="cap-sub">BTC 1h <b class="'+((btc1Pnl||0)>=0?'up':'dn')+'">'+usd(btc1Pnl)+'</b>'
    +' · 15m <b class="'+((btc15Pnl||0)>=0?'up':'dn')+'">'+usd(btc15Pnl)+'</b></div>'
    +'<div class="cap-sub">ETH 1h <b class="'+((eth1Pnl||0)>=0?'up':'dn')+'">'+usd(eth1Pnl)+'</b>'
    +' · 15m <b class="'+((eth15Pnl||0)>=0?'up':'dn')+'">'+usd(eth15Pnl)+'</b></div>'
    +'<div class="cap-sub">Combined <b class="'+((dirPnl||0)>=0?'up':'dn')+'">'+usd(dirPnl)+'</b> · '
    +f(laneTotal,0)+' trades</div>';

  renderStats(document.getElementById('stats-bar'),lanes,cap,s);

  const rows=buildRows({...s,lane_stats:lanes});
  const ov=overallLight({...s,lane_stats:lanes});
  const v=document.getElementById('verdict');
  v.innerHTML=dot(ov.light)+'<span>'+ov.text+'</span>';
  v.style.borderColor=ov.light==='green'?'rgba(74,222,128,.4)':(ov.light==='yellow'?'rgba(250,204,21,.4)':'rgba(248,113,113,.4)');
  renderLanePanels(document.getElementById('lane-panels'),(l&&l.lane_trades)||{},lanes,{
    btc_1h:btc1Pnl,
    btc_15m:btc15Pnl,
    eth_1h:eth1Pnl,
    eth_15m:eth15Pnl,
  });
  renderRows(document.getElementById('tl-grid'),rows);
}
tick();setInterval(tick,60000);
</script>
</body>
</html>"""