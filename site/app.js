/* AlphaNode site interactions — vanilla JS, no dependencies.
   Everything degrades gracefully: no JS -> static page; reduced motion -> no animation. */
(() => {
  'use strict';
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  document.documentElement.classList.add('js');

  /* ---------- nav: solidify on scroll ---------- */
  const nav = document.querySelector('nav');
  const onScroll = () => nav && nav.classList.toggle('scrolled', scrollY > 10);
  addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  /* ---------- scroll reveal (staggered) ---------- */
  const targets = document.querySelectorAll('.card, .reveal, section h2, p.lead, .stat');
  if (reduced || !('IntersectionObserver' in window)) {
    targets.forEach(el => el.classList.add('in'));
  } else {
    targets.forEach(el => {
      el.classList.add('reveal');
      const sibs = el.parentElement ? [...el.parentElement.children] : [el];
      el.style.transitionDelay = `${(sibs.indexOf(el) % 6) * 70}ms`;
    });
    const io = new IntersectionObserver(entries => {
      for (const e of entries) if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); }
    }, { threshold: 0.12, rootMargin: '0px 0px -6% 0px' });
    targets.forEach(el => io.observe(el));
  }

  /* ---------- count-up stats ---------- */
  const counters = document.querySelectorAll('[data-count]');
  const runCounter = el => {
    const end = parseFloat(el.dataset.count);
    const dec = +(el.dataset.decimals || 0);
    const pre = el.dataset.prefix || '', suf = el.dataset.suffix || '';
    const t0 = performance.now(), dur = 1400;
    const fmt = v => pre + v.toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec }) + suf;
    if (reduced) { el.textContent = fmt(end); return; }
    const tick = now => {
      const p = Math.min(1, (now - t0) / dur), ease = 1 - Math.pow(1 - p, 3);
      el.textContent = fmt(end * ease);
      if (p < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  };
  if ('IntersectionObserver' in window && !reduced) {
    const cio = new IntersectionObserver(es => {
      for (const e of es) if (e.isIntersecting) { runCounter(e.target); cio.unobserve(e.target); }
    }, { threshold: 0.6 });
    counters.forEach(el => cio.observe(el));
  } else counters.forEach(runCounter);

  /* ---------- hero: drifting node network ---------- */
  const cv = document.getElementById('net');
  if (cv && !reduced) {
    const ctx = cv.getContext('2d');
    let W, H, pts = [], raf = 0;
    const DPR = Math.min(devicePixelRatio || 1, 2);
    const N = innerWidth < 700 ? 26 : 46, LINK = 150;
    const resize = () => {
      const r = cv.parentElement.getBoundingClientRect();
      W = r.width; H = r.height;
      cv.width = W * DPR; cv.height = H * DPR;
      cv.style.width = W + 'px'; cv.style.height = H + 'px';
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    };
    const seed = () => {
      pts = Array.from({ length: N }, () => ({
        x: Math.random() * W, y: Math.random() * H,
        vx: (Math.random() - .5) * .35, vy: (Math.random() - .5) * .35,
        r: 1 + Math.random() * 1.8, hot: Math.random() < .18,
      }));
    };
    const step = () => {
      ctx.clearRect(0, 0, W, H);
      for (const p of pts) {
        p.x += p.vx; p.y += p.vy;
        if (p.x < -10) p.x = W + 10; else if (p.x > W + 10) p.x = -10;
        if (p.y < -10) p.y = H + 10; else if (p.y > H + 10) p.y = -10;
      }
      for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++) {
        const a = pts[i], b = pts[j], dx = a.x - b.x, dy = a.y - b.y, d = dx * dx + dy * dy;
        if (d < LINK * LINK) {
          const o = (1 - Math.sqrt(d) / LINK) * .34;
          ctx.strokeStyle = `rgba(139,124,255,${o})`;
          ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        }
      }
      for (const p of pts) {
        ctx.fillStyle = p.hot ? 'rgba(139,124,255,.95)' : 'rgba(110,168,255,.55)';
        ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, 7); ctx.fill();
        if (p.hot) {
          ctx.fillStyle = 'rgba(139,124,255,.12)';
          ctx.beginPath(); ctx.arc(p.x, p.y, p.r * 4, 0, 7); ctx.fill();
        }
      }
      raf = requestAnimationFrame(step);
    };
    const start = () => { cancelAnimationFrame(raf); resize(); if (!pts.length) seed(); raf = requestAnimationFrame(step); };
    addEventListener('resize', () => { resize(); seed(); }, { passive: true });
    document.addEventListener('visibilitychange', () =>
      document.hidden ? cancelAnimationFrame(raf) : start());
    start();
  }

  /* ---------- hero: live mining terminal ---------- */
  const log = document.getElementById('termlog');
  if (log) {
    let round = 127;
    const lines = () => [
      [`▶ round ${++round} · 1h bars · 60 pairs`, 'c'],
      ['  gen 12/25 · best fit 1.38 · dd −9.2%', ''],
      ['  ♦ champion cs_rank(ts_roc:30(close))', 'a'],
      ['  ⚙ window polish: 3/5 champions improved', 'b'],
      ['  ✓ library +2 → 511 alphas', 'g'],
      ['  ▤ portfolio top-6 · TEST Sharpe +2.1', 'g'],
    ];
    if (reduced) {
      log.innerHTML = lines().map(([t, c]) => `<span class="${c}">${t}</span>`).join('\n');
    } else {
      let queue = [], out = [], typing = '', ci = 0;
      const render = () =>
        log.innerHTML = out.concat(`${typing}<span class="caret">▌</span>`).join('\n');
      const tick = () => {
        if (!queue.length) queue = lines();
        const [text, cls] = queue[0];
        ci += 1 + (Math.random() < .3 ? 1 : 0);
        typing = `<span class="${cls}">${text.slice(0, ci)}</span>`;
        if (ci >= text.length) {
          out.push(`<span class="${cls}">${text}</span>`);
          if (out.length > 5) out.shift();
          queue.shift(); typing = ''; ci = 0;
          render();
          setTimeout(tick, 420 + Math.random() * 500);
        } else { render(); setTimeout(tick, 14 + Math.random() * 22); }
      };
      tick();
    }
  }
})();
