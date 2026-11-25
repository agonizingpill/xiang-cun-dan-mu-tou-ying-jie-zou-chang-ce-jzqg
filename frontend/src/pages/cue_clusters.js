import {awaitCompute, request} from '../api.js';
import {el} from '../dom.js';
import {evidence} from '../store.js';
import {dataView, status, titleBlock} from '../widgets.js';

export const title = '节奏相近的对场 cue';
export const key = 'cue_clusters';

export const page = {
  title,
  key,
  path: '/cue_clusters',
  caption: '对场节奏簇',
  detail: '两位领班分别从同一次归簇的原始成员中挑样板或判定不合用；交齐前选择保密，分歧由未投过票的第三位领班按原候选裁决。',
  layout: 'split',
  view: 'grid',
  columns: [
    {name: 'code', label: '编号'},
    {name: 'track', label: '输入素材'},
    {name: 'representative', label: '签出样板'},
    {name: 'distance', label: 'DTW 距离'},
    {name: 'members', label: '簇内轨迹'},
    {name: 'token', label: '计算凭据'}
  ],
  actions: [
    {
      name: 'compare_cues',
      label: '计算可迁移节奏簇',
      path: '/cue_clusters/compare-cues',
      fields: [{name: 'track', label: '试投素材', kind: 'text', initial: 'CUE-FINAL'}],
      roles: ['operator', 'reviewer']
    },
    {
      name: 'save_cluster',
      label: '保留对场节奏凭据',
      path: '/cue_clusters/save-cluster',
      fields: [
        {name: 'code', label: '节奏簇编号', kind: 'text', initial: 'BEAT-24'},
        {name: 'token', label: '任务', kind: 'token', initial: ''}
      ],
      roles: ['reviewer']
    },
    {name: 'vote_cluster', label: '领班挑样板', path: '/cue_clusters/vote', roles: ['reviewer']},
    {name: 'adjudicate_cluster', label: '第三位领班裁决', path: '/cue_clusters/adjudicate-cluster', roles: ['reviewer']}
  ]
};

function voterId(input) {
  return input.value.trim();
}

function candidateChoices(candidates, groupName = 'representative') {
  const options = candidates.map(code => el('label', {class: 'choice-option'},
    el('input', {type: 'radio', name: groupName, value: code, 'data-candidate': code}),
    el('span', {}, code)));
  options.push(el('label', {class: 'choice-option unusable'},
    el('input', {type: 'radio', name: groupName, value: '__unusable__', 'data-unusable': 'true'}),
    el('span', {}, '判不合用')));
  return el('fieldset', {class: 'choice-grid'},
    el('legend', {}, '簇样板：只能选择同一次归簇当时的候选'), options);
}

function selectedChoice(form, groupName) {
  const data = new FormData(form);
  const value = data.get(groupName);
  if (!value) throw new Error('请选择一条样板，或判定不合用');
  if (value === '__unusable__') return {usable: false};
  return {representative: String(value), usable: true};
}

function voteText(vote) {
  if (!vote) return '尚未投票';
  if (vote.hidden) return '已提交；交齐前选择保密';
  return vote.usable === false ? '判定不合用' : `选择 ${vote.choice || vote.representative}`;
}

function identityPayload(voterInput) {
  const voter = voterId(voterInput);
  return voter ? {voter} : {};
}

export async function render(root, role) {
  const isReviewer = role === 'reviewer';
  let current = null;

  const clusterNotice = status('正在读取节奏簇…');
  const clusterPanel = el('section', {class: 'ledger', 'aria-label': '对场节奏簇台账'},
    el('h2', {}, '对场节奏簇台账'), clusterNotice);

  async function loadClusters() {
    try {
      const rows = await request('/cue_clusters');
      clusterPanel.replaceChildren(
        el('h2', {}, '对场节奏簇台账'),
        rows.length
          ? dataView(rows, [
              {name: 'code', label: '编号'},
              {name: 'track', label: '输入素材'},
              {name: 'representative', label: '签出样板'},
              {name: 'distance', label: 'DTW 距离'},
              {name: 'members', label: '簇内轨迹'},
              {name: 'token', label: '计算凭据'}
            ], 'grid')
          : status('暂无已签出的节奏簇；双人意见一致或第三位裁决合用后会一同落定。')
      );
    } catch (error) {
      clusterPanel.replaceChildren(el('h2', {}, '对场节奏簇台账'), status(error.message, 'error'));
    }
  }

  const codeInput = el('input', {name: 'code', value: 'BEAT-24', required: '', maxlength: '64'});
  const tokenInput = el('input', {name: 'token', value: evidence.get(), placeholder: 'compare-cues 返回的任务号'});
  const storedVoterKey = 'cue-cluster-voter';
  const storedVoter = (() => {
    try {
      // sessionStorage gives each newly opened foreman tab its own identity;
      // a reload/retry in the same tab keeps the same ballot identity.
      const existing = globalThis.sessionStorage?.getItem(storedVoterKey);
      if (existing) return existing;
      const generated = `foreman-${Math.random().toString(36).slice(2, 10)}`;
      globalThis.sessionStorage?.setItem(storedVoterKey, generated);
      return generated;
    } catch {
      return '';
    }
  })();
  const voterInput = el('input', {
    name: 'voter',
    value: storedVoter,
    placeholder: '领班工号/姓名；不同领班请填不同标识'
  });
  voterInput.addEventListener('change', () => {
    try { globalThis.sessionStorage?.setItem(storedVoterKey, voterInput.value.trim()); } catch { /* private mode */ }
  });
  const reviewBody = el('div', {class: 'review-body'});

  async function loadReview() {
    const code = codeInput.value.trim();
    if (!code) throw new Error('请填写节奏簇编号');
    const params = new URLSearchParams({code});
    const voter = voterId(voterInput);
    if (voter) params.set('voter', voter);
    current = await request('/cue_clusters/review-cluster?' + params.toString());
    renderReview();
    return current;
  }

  async function postAction(path, payload, notice, successText) {
    notice.className = 'status';
    notice.textContent = '提交中…';
    try {
      current = await request(path, payload);
      renderReview();
      await loadClusters();
      notice.textContent = successText;
      notice.classList.add('ok');
    } catch (error) {
      notice.textContent = error.message;
      notice.classList.add('error');
    }
  }

  function renderReview() {
    if (!current) {
      reviewBody.replaceChildren(status('开启案件后，这里会显示原始候选和当前审签状态。'));
      return;
    }

    const notice = status(current.message || current.status_label || '');
    if (current.closed) notice.classList.add('ok');

    const summary = el('div', {class: 'review-summary'},
      el('h3', {}, `${current.code} · ${current.status_label || current.status}`),
      el('p', {}, `候选：${(current.candidates || []).join('、') || '—'}`),
      el('p', {}, `已交票数：${current.vote_count ?? (current.votes || []).length} / 2`),
      current.representative
        ? el('p', {}, `签出样板：${current.representative}；距离：${current.distance ?? '—'}`)
        : null);

    const votes = el('ul', {class: 'vote-list', 'aria-label': '投票状态'},
      (current.votes || []).map((vote, index) =>
        el('li', {'data-vote-hidden': vote.hidden ? 'true' : 'false'},
          `第 ${index + 1} 票：${voteText(vote)}`)));

    const blocks = [summary, votes, notice];

    if (current.your_vote) {
      blocks.push(el('p', {class: 'your-vote', 'data-your-vote': ''},
        `你的选择：${voteText(current.your_vote)}。重复提交不会重复计票。`));
    }

    if (!isReviewer) {
      blocks.push(status('当前角色只读；请使用 reviewer 账号执行挑样板和裁决。'));
    } else if (current.status === 'disputed' && current.can_adjudicate) {
      const adjudicateNotice = status('');
      const form = el('form', {class: 'action adjudicate-form', id: 'adjudicate_cluster'},
        el('h3', {}, '第三位领班按原候选裁决'),
        el('p', {}, '你必须是没有投过票的第三位领班。即使原比较任务已过期，仍使用本案件冻结的候选。'),
        candidateChoices(current.candidates || [], 'representative'),
        el('button', {type: 'submit'}, '提交裁决并签簇'),
        adjudicateNotice);
      form.addEventListener('submit', event => {
        event.preventDefault();
        let choice;
        try { choice = selectedChoice(form, 'representative'); }
        catch (error) {
          adjudicateNotice.textContent = error.message;
          adjudicateNotice.className = 'status error';
          return;
        }
        postAction('/cue_clusters/adjudicate-cluster', {
          code: current.code,
          ...identityPayload(voterInput),
          ...choice
        }, adjudicateNotice, '裁决已落定；合用裁决已同时签簇。');
      });
      blocks.push(form);
    } else if (!current.closed && current.can_vote && !current.your_vote) {
      const voteNotice = status('');
      const form = el('form', {class: 'action vote-form', id: 'vote_cluster'},
        el('h3', {}, '领班挑样板'),
        el('p', {}, '提交后、两人交齐前，对方看不到你的选择；网络重试不会重复计票。'),
        candidateChoices(current.candidates || [], 'representative'),
        el('button', {type: 'submit'}, '提交我的选择'),
        voteNotice);
      form.addEventListener('submit', event => {
        event.preventDefault();
        let choice;
        try { choice = selectedChoice(form, 'representative'); }
        catch (error) {
          voteNotice.textContent = error.message;
          voteNotice.className = 'status error';
          return;
        }
        postAction('/cue_clusters/vote', {
          code: current.code,
          token: current.token || tokenInput.value.trim(),
          ...identityPayload(voterInput),
          ...choice
        }, voteNotice, '已收到你的一票；交齐前选择保密。');
      });
      blocks.push(form);
    } else if (!current.closed) {
      blocks.push(status(current.status === 'disputed'
        ? '分歧已成立；请由未投过票的第三位领班读取/提交裁决。'
        : '已记录你的选择，等待另一位领班交齐。'));
    }

    if (current.closed && current.cluster_record) {
      blocks.push(el('p', {class: 'status ok', 'data-signed-cluster': current.code},
        `节奏簇 ${current.code} 已签出，距离按样板 ${current.representative} 计算。`));
    }

    reviewBody.replaceChildren(...blocks);
  }

  const setupNotice = status('');
  const setupForm = el('form', {class: 'action setup-form', id: 'save_cluster'},
    el('h3', {}, '保留对场节奏凭据：开启双人审签'),
    el('p', {}, '先运行比较，再用同一任务号冻结当时的成员候选；输出供放映领班查阅。'),
    el('div', {class: 'fields'},
      el('label', {}, el('span', {}, '节奏簇编号'), codeInput),
      el('label', {}, el('span', {}, '任务 / 计算凭据 token'), tokenInput),
      el('label', {}, el('span', {}, '本机领班标识（多人/多窗口须不同）'), voterInput)),
    el('div', {class: 'button-row'},
      el('button', {type: 'submit'}, '保留对场节奏凭据 / 开启双人审签'),
      el('button', {type: 'button', class: 'quiet', onclick: () => loadReview().catch(error => {
        setupNotice.textContent = error.message;
        setupNotice.className = 'status error';
      })}, '刷新审签状态')),
    setupNotice);
  setupForm.addEventListener('submit', event => {
    event.preventDefault();
    setupNotice.className = 'status';
    setupNotice.textContent = '正在冻结候选…';
    request('/cue_clusters/open-review', {
      code: codeInput.value.trim(),
      token: tokenInput.value.trim(),
      ...identityPayload(voterInput)
    }).then(state => {
      current = state;
      renderReview();
      setupNotice.textContent = '候选已按该次归簇冻结。';
      setupNotice.classList.add('ok');
    }).catch(error => {
      setupNotice.textContent = error.message;
      setupNotice.classList.add('error');
    });
  });

  const compareNotice = status('');
  const trackInput = el('input', {name: 'track', value: 'CUE-FINAL', required: ''});
  const compareForm = el('form', {class: 'action compare-form', id: 'compare_cues'},
    el('h3', {}, '计算可迁移节奏簇'),
    el('p', {}, '返回最近代表 cue 与簇内成员；这些成员就是两位领班的原始候选。'),
    el('label', {}, el('span', {}, '试投素材'), trackInput),
    el('button', {type: 'submit'}, '计算可迁移节奏簇'),
    compareNotice);
  compareForm.addEventListener('submit', event => {
    event.preventDefault();
    const button = compareForm.querySelector('button');
    button.disabled = true;
    compareNotice.className = 'status';
    compareNotice.textContent = '计算任务提交中…';
    request('/cue_clusters/compare-cues', {track: trackInput.value.trim()})
      .then(data => {
        evidence.save(data.token);
        tokenInput.value = data.token;
        return awaitCompute(data.token, state => {
          compareNotice.textContent = '任务状态：' + (state.status || '处理中');
        });
      })
      .then(result => {
        if (result.status === 'rejected') throw new Error(result.detail || '计算被拒绝');
        compareNotice.textContent = '计算完成，任务号已填入审签表单。';
        compareNotice.classList.add('ok');
      })
      .catch(error => {
        compareNotice.textContent = error.message;
        compareNotice.classList.add('error');
      })
      .finally(() => { button.disabled = false; });
  });

  const actionPanel = el('section', {class: 'actions', 'aria-label': '节奏簇审签操作'},
    role === 'operator' || role === 'reviewer' ? compareForm : null,
    isReviewer ? setupForm : status('只读访客可浏览台账；挑样板和裁决需要 reviewer 权限。'),
    el('section', {class: 'action review-panel', 'aria-label': '双人挑选与第三人裁决'},
      el('h3', {}, '审签进度'), reviewBody));

  root.replaceChildren(
    titleBlock(page.module || '对场节奏簇', title, page.detail),
    el('div', {class: 'work cue-cluster-work'}, clusterPanel, actionPanel)
  );

  renderReview();
  await loadClusters();
}
