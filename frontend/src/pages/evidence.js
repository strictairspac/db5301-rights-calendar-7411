import {heading, panel} from '../components/common/heading.js';
import {entries, records} from '../components/common/records.js';
import {topic} from '../constants/project.js';
import {date, el, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

function runCaseId(state, runId) {
  return state.runs.find(row=>row.id===runId)?.case_id || 0;
}

function cleanupPanel(state) {
  const isOperator = state.session.role==='operator';
  const rows = state.cases.map(row=>{
    const signed = state.evidence.filter(item=>item.reviewer && runCaseId(state, item.run_id)===row.id);
    const status = row.cleaned_at
      ? `<span class="status" data-state="cleaned" data-testid="cleaned-badge">已清理 · ${esc(date(row.cleaned_at))}</span>`
      : '<span class="status" data-state="active">在档</span>';
    const action = !row.cleaned_at && isOperator
      ? `<form class="cleanup-form" data-case="${row.id}"><button class="secondary" data-testid="cleanup-${row.id}">发起清理</button></form>`
      : (row.cleaned_at ? '<span class="note">原始材料已清除</span>' : '—');
    return [esc(row.title), status, esc(String(signed.length)), action];
  });
  return panel('档案到期清理', records(['档案', '清理状态', '有效签署报告', '操作'], rows,
    '尚未建立档案。') + '<p class="note">清理会在单个事务内清除该档案的原始排片、授权输入和未签报告；已签报告按原编号留存，清理不等待后台校核，晚到结论不会重新入库。</p>');
}

function signedPanel(state) {
  const signed = state.evidence.filter(row=>row.reviewer);
  const caseTitle = new Map(state.cases.map(row=>[row.id, row.title]));
  const rows = signed.map(row=>[
    `<a href="#evidence/${row.id}">报告 ${esc(row.id)}</a>`,
    esc(caseTitle.get(runCaseId(state, row.run_id)) || runCaseId(state, row.run_id)),
    esc(row.reviewer), esc(date(row.reviewed_at))]);
  return panel('仍有效的签署报告', records(['报告编号', '所属档案', '签署人', '签署时间'], rows,
    '当前没有已签署报告。'));
}

export function evidencePage(state, identity) {
  const current = state.evidence.find(row=>row.id===identity) || state.evidence[0];
  if (!current) return `${heading(topic.modules[3])}<p class="empty">执行完成后，可查看、复核和导出证据。</p>${cleanupPanel(state)}${signedPanel(state)}`;
  const {report} = current;
  const metrics = Object.entries(report.metrics).map(([label,value])=>`<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('');
  const sections = report.sections.map(x=>panel(x.title, entries(x.rows))).join('');
  const review = current.reviewer ? `<p data-testid="reviewed">${esc(current.reviewer)} · ${esc(date(current.reviewed_at))}</p><p>${esc(current.review_note)}</p>`
    : state.session.role==='reviewer'?'<form id="review-form"><label>复核意见<textarea name="note" maxlength="500" required></textarea></label><button>签署复核</button></form>'
    : '<p class="note">待复核员签署</p>';
  const history = state.evidence.map(row=>[`<a href="#evidence/${row.id}">执行 ${row.run_id}</a>`, esc(row.report.summary), esc(row.reviewer || '待复核')]);
  return `${heading(topic.modules[3], report.summary, `<button id="download" data-id="${current.id}">导出证据</button>`)}
    <div class="spread evidence-top"><dl class="metrics">${metrics}</dl><aside class="brief"><h2>复核签署</h2>${review}<details><summary>结果校验码</summary><code class="digest">${esc(current.digest)}</code></details></aside></div>
    ${sections}<details><summary>历史证据</summary>${records(['执行','结论','复核'], history)}</details>${cleanupPanel(state)}${signedPanel(state)}`;
}

export function bindEvidence(api, update, fail) {
  document.querySelectorAll('form.cleanup-form').forEach(form=>{
    form.addEventListener('submit', async event=>{
      event.preventDefault();
      const button = event.submitter;
      const caseId = form.dataset.case;
      if (!window.confirm('确认清理该档案？原始排片、授权输入和未签报告将从库中删除，已签报告保留。')) return;
      if (button) button.disabled = true;
      try {
        await api(`/cases/${caseId}/cleanup`, {method:'POST'});
        await update();
      } catch (error) { fail(error); }
      finally { if (button) button.disabled = false; }
    });
  });
  const button = document.getElementById('download');
  if (!button) return;
  const identity = button.dataset.id;
  button.addEventListener('click', async()=>{
    try {
      const response = await api(`/evidence/${identity}/download`, {response:true});
      const url = URL.createObjectURL(await response.blob());
      const link = el('a');
      link.href = url;
      link.download = `evidence-${identity}.json`;
      link.click();
      setTimeout(()=>URL.revokeObjectURL(url), 1000);
    } catch (error) { fail(error); }
  });
  submit('review-form', async data=>{
    await api(`/evidence/${identity}/review`, {method:'POST', data:{note:data.get('note')}});
    await update();
  }, fail);
}
