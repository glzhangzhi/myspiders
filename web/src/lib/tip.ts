// 即时悬浮提示：给元素加 data-tip="第一行\n第二行" 即可，事件委托、零延迟。
// 原生 title 有约 1 秒延迟，用在图表柱子这类窄元素上扫一遍要一根根等，所以图表一律走这里。
// 与旧页面 app/dashboard/common.js 的 initTipLayer 同一套行为（iframe 内的旧页面用那份）。
// 文本走 textContent，不接受 HTML。

let el: HTMLDivElement | null = null;

function ensure(): HTMLDivElement {
  if (el) return el;
  el = document.createElement('div');
  el.className = 'tip';
  document.body.appendChild(el);
  return el;
}

function place(ev: MouseEvent) {
  const t = ensure();
  const pad = 12;
  const r = t.getBoundingClientRect();
  // 默认在光标右上方；贴边时翻转，避免被视口裁掉。
  let x = ev.clientX + pad;
  let y = ev.clientY - r.height - pad;
  if (x + r.width > window.innerWidth - 6) x = ev.clientX - r.width - pad;
  if (y < 6) y = ev.clientY + pad;
  if (x < 6) x = 6;
  t.style.left = `${x}px`;
  t.style.top = `${y}px`;
}

const tipTarget = (ev: Event) => (ev.target instanceof Element ? ev.target.closest<HTMLElement>('[data-tip]') : null);

function hide() { el?.classList.remove('on'); }

export function installTips() {
  document.addEventListener('mouseover', (ev) => {
    const target = tipTarget(ev);
    const text = target?.dataset.tip;
    if (!text) return;
    const t = ensure();
    t.textContent = text;
    t.classList.add('on');
    place(ev);
  });
  document.addEventListener('mousemove', (ev) => { if (el?.classList.contains('on') && tipTarget(ev)) place(ev); });
  document.addEventListener('mouseout', (ev) => {
    const target = tipTarget(ev);
    if (target && !target.contains(ev.relatedTarget as Node | null)) hide();
  });
  // 滚动/切页时元素可能移走，留着的提示会变成孤儿
  window.addEventListener('scroll', hide, true);
  window.addEventListener('blur', hide);
}
