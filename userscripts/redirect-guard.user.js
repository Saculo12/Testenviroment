// ==UserScript==
// @name         Redirect & Popup Guard
// @description  Blocks popups, popunders, ad overlays and off-site redirects on bingeflix (incl. its video players), and hides "Open in app" banners on all sites
// @version      2.3
// @match        *://*/*
// @run-at       document-start
// @inject-into  page
// ==/UserScript==

// Why @match *://*/* : most of the remaining redirects come from the video
// player, which is an <iframe> on a different domain. The script has to run
// inside that iframe too. It exits immediately on any page that is not
// bingeflix or embedded in bingeflix (only the "Open in app" banner blocker
// runs on other sites).

(function () {
  'use strict';

  // ---- Settings ---------------------------------------------------------
  const SITE = /bingeflix/i;   // site(s) to protect (matched against hostnames)

  // Sandbox embedded players so they physically cannot open popups or
  // redirect the whole tab. This is the strongest protection. If the player
  // shows "sandbox detected" / "please disable sandbox" or won't play,
  // set this to false (the other layers still run inside the player).
  const SANDBOX_PLAYERS = false;

  // Remove invisible full-screen layers that eat your first tap.
  const REMOVE_OVERLAYS = true;

  // Domains that are always allowed to open / navigate. If a legit link or
  // the player itself gets blocked, add its domain here, e.g. 'vidplayer.com'.
  const EXTRA_ALLOW = [];

  // Ad / popunder networks whose scripts and frames are refused outright.
  const AD_HOSTS = /popads|popcash|popunder|propeller|adsterra|hilltop|monetag|exoclick|exosrv|juicyads|clickadu|admaven|ad-maven|adcash|onclicka|richads|galaksion|trafficstars|tsyndicate|rollerads|a-ads|acint\.net|cpm|syndication|doubleclick|adnxs|outbrain|taboola|mgid|revcontent|bidgear|realsrv|magsrv|pushame|zeroredirect|redirect|aclib|histats|dtscout|crwdcntrl/i;

  // Hide "Open in app" / "Get the app" banners on EVERY website (not just
  // bingeflix). Set to false to turn this off.
  const BLOCK_APP_BANNERS = true;

  let inFrame;
  try { inFrame = window.top !== window.self; } catch (_) { inFrame = true; }

  // ---- "Open in app" banners (all sites) ------------------------------------
  if (BLOCK_APP_BANNERS && !inFrame && !window.__appBannerGuard) {
    Object.defineProperty(window, '__appBannerGuard', { value: true });

    // a) Safari's built-in Smart App Banner comes from this <meta> tag.
    const APP_META = 'meta[name="apple-itunes-app" i],meta[name="google-play-app" i],meta[name="apple-itunes-app-id" i]';

    // b) Home-made banners that sites build themselves.
    const BANNER_CSS =
      '#smartbanner,.smartbanner,.smart-banner,[class*="smart-app-banner" i],' +
      '[class*="app-banner" i],[id*="app-banner" i],[class*="appbanner" i],' +
      '[class*="open-in-app" i],[id*="open-in-app" i],[class*="openinapp" i],' +
      '[class*="xpromo" i],[class*="get-the-app" i],[class*="download-app" i],' +
      '[class*="app-promo" i],[class*="apppromo" i]' +
      '{display:none !important}';

    const addStyle = () => {
      const root = document.head || document.documentElement;
      if (!root) return false;
      const s = document.createElement('style');
      s.textContent = BANNER_CSS;
      root.appendChild(s);
      return true;
    };

    // c) Anything else: a short "Open app" / "Continue in app" button inside a
    //    fixed or sticky bar or bottom sheet gets that bar hidden.
    const APP_TEXT = /\b(open|continue|view|use|switch to|read|watch|listen)\b.{0,15}\bapp\b|\bget the app\b|\bdownload (the|our) app\b|\bopen in\b.{0,15}\bapp\b/i;
    const seen = new WeakSet();

    function hideBanner(btn) {
      let el = btn;
      for (let i = 0; i < 10 && el && el !== document.body && el !== document.documentElement; i++, el = el.parentElement) {
        const pos = getComputedStyle(el).position;
        if (pos !== 'fixed' && pos !== 'sticky') continue;
        // Don't hide a whole site header/menu that just happens to contain
        // an "app" button: only small, mostly-empty bars.
        const target = el.textContent.trim().length < 300 ? el : btn;
        target.style.setProperty('display', 'none', 'important');
        // Bottom sheets often lock page scrolling; unlock it.
        for (const r of [document.body, document.documentElement]) {
          if (r && getComputedStyle(r).overflow === 'hidden') r.style.setProperty('overflow', 'auto', 'important');
        }
        console.log('[Guard] hid app banner:', btn.textContent.trim());
        return;
      }
    }

    function scanBanners() {
      for (const m of document.querySelectorAll(APP_META)) m.remove();
      if (!document.body) return;
      for (const b of document.querySelectorAll('a,button,[role="button"]')) {
        if (seen.has(b)) continue;
        seen.add(b);
        const t = (b.textContent || '').trim();
        if (t && t.length <= 40 && APP_TEXT.test(t)) hideBanner(b);
      }
    }

    // Remove the tag right away if the page already has it, and again once
    // the page has loaded (in case Safari checks for it then).
    const killMeta = () => { for (const m of document.querySelectorAll(APP_META)) m.remove(); };
    killMeta();
    document.addEventListener('DOMContentLoaded', killMeta, true);
    window.addEventListener('load', killMeta, true);

    let styled = addStyle();
    let pending = false;
    new MutationObserver((muts) => {
      if (!styled) styled = addStyle();
      for (const m of muts) for (const n of m.addedNodes) {
        if (n.nodeType === 1 && n.matches(APP_META)) n.remove(); // before Safari reads it
      }
      if (pending) return;
      pending = true;
      setTimeout(() => { pending = false; scanBanners(); }, 300);
    }).observe(document, { childList: true, subtree: true });
  }

  // ---- Popup / redirect guard: only on the site or frames embedded by it ---
  const ancestors = location.ancestorOrigins ? Array.from(location.ancestorOrigins) : [];
  const onSite = SITE.test(location.hostname);
  const underSite = inFrame && (ancestors.some((o) => SITE.test(o)) || SITE.test(document.referrer));
  if (!onSite && !underSite) return;
  if (window.__redirectGuard) return;
  Object.defineProperty(window, '__redirectGuard', { value: true });

  // ---- Allowlist ----------------------------------------------------------
  const baseOf = (h) => h.replace(/^www\./, '').split('.').slice(-2).join('.');
  const hostOf = (u) => { try { return new URL(String(u), location.href).hostname; } catch (_) { return ''; } };

  const ALLOW = new Set([baseOf(location.hostname), ...EXTRA_ALLOW]);
  if (ancestors.length) ALLOW.add(baseOf(hostOf(ancestors[ancestors.length - 1])));

  // Harmless schemes. Anything else that isn't http(s) (itms-apps:, intent:,
  // market:, ...) is an App Store / app redirect and gets blocked.
  const SAFE_SCHEMES = /^(about|javascript|blob|data|mailto|tel):$/;

  function allowed(url) {
    if (url == null || url === '') return true;
    let u;
    try { u = new URL(String(url), location.href); } catch (_) { return true; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return SAFE_SCHEMES.test(u.protocol);
    const h = u.hostname.replace(/^www\./, '');
    if (SITE.test(h)) return true;
    for (const a of ALLOW) if (h === a || h.endsWith('.' + a)) return true;
    return false;
  }

  function isAdUrl(url) {
    const h = hostOf(url);
    return !!h && !SITE.test(h) && AD_HOSTS.test(h);
  }

  // ---- On-screen counter --------------------------------------------------
  let count = 0;
  function log(what, url) {
    count++;
    console.log('[Guard] blocked ' + what + ':', url);
    if (!document.body) return;
    let t = document.getElementById('__guard_toast');
    if (!t) {
      t = document.createElement('div');
      t.id = '__guard_toast';
      Object.assign(t.style, {
        position: 'fixed', bottom: '14px', left: '50%', transform: 'translateX(-50%)',
        zIndex: '2147483647', background: 'rgba(0,0,0,.8)', color: '#fff',
        font: '12px -apple-system, sans-serif', padding: '6px 10px',
        borderRadius: '8px', pointerEvents: 'none', transition: 'opacity .3s'
      });
      document.body.appendChild(t);
    }
    t.textContent = '🛡 Blocked ' + what + ' (' + count + ')';
    t.style.opacity = '1';
    clearTimeout(t._h);
    t._h = setTimeout(() => { t.style.opacity = '0'; }, 1500);
  }

  // ---- Make patched functions look native ---------------------------------
  // Many ad scripts check `window.open.toString()` for "[native code]" and,
  // if it's been replaced, fall back to redirecting the whole tab.
  const disguised = new WeakMap();
  const nativeToString = Function.prototype.toString;
  const fakeToString = function toString() {
    return disguised.has(this) ? disguised.get(this) : nativeToString.call(this);
  };
  disguised.set(fakeToString, nativeToString.call(nativeToString));
  try {
    Object.defineProperty(Function.prototype, 'toString', { value: fakeToString, writable: true, configurable: true });
  } catch (_) {}

  function disguise(fn, orig) { disguised.set(fn, nativeToString.call(orig)); return fn; }

  function patchMethod(obj, name, make) {
    const orig = obj && obj[name];
    if (typeof orig !== 'function') return;
    const fn = disguise(make(orig), orig);
    try { Object.defineProperty(obj, name, { value: fn, writable: true, configurable: true }); }
    catch (_) { try { obj[name] = fn; } catch (__) {} }
  }

  function patchSetter(proto, prop, make) {
    const d = proto && Object.getOwnPropertyDescriptor(proto, prop);
    if (!d || !d.set) return;
    const set = disguise(make(d.set), d.set);
    try { Object.defineProperty(proto, prop, { ...d, set }); } catch (_) {}
  }

  function patchGetter(proto, prop, make) {
    const d = proto && Object.getOwnPropertyDescriptor(proto, prop);
    if (!d || !d.get) return;
    const get = disguise(make(d.get), d.get);
    try { Object.defineProperty(proto, prop, { ...d, get }); } catch (_) {}
  }

  // A fake window handed back for blocked popups, so ad scripts believe the
  // popup opened and don't fall back to redirecting this tab.
  const fakeWin = new Proxy(function () {}, {
    get: (_, p) => {
      if (p === 'closed') return false;
      if (p === 'then') return undefined;
      if (p === Symbol.toPrimitive) return () => '';
      return fakeWin;
    },
    set: () => true,
    apply: () => fakeWin
  });

  const linkOf = (el) =>
    el && el.closest ? el.closest('a[href],area[href]') : null;

  // ---- Lock down a window (this page, and same-origin child frames) -------
  // Ad scripts often create a blank <iframe> just to grab an unpatched
  // window.open / click() from it, so every same-origin frame gets the same
  // treatment as the main page.
  function guardRealm(w) {
    try {
      if (!w || w.__guardRealm) return;
      Object.defineProperty(w, '__guardRealm', { value: true });
    } catch (_) { return; } // cross-origin window: nothing we can do from here

    // 1. Popups / popunders
    const realOpen = w.open;
    const open = disguise(function open(url, ...rest) {
      if (url && allowed(url)) return realOpen.call(w, url, ...rest);
      log('popup', url || '(blank)');
      return fakeWin;
    }, realOpen);
    try { Object.defineProperty(w, 'open', { value: open, writable: false, configurable: false }); }
    catch (_) { try { w.open = open; } catch (__) {} }

    // 2. Scripted clicks on hidden off-site links (element.click())
    patchMethod(w.HTMLElement && w.HTMLElement.prototype, 'click', (orig) => function click() {
      const a = linkOf(this);
      if (a && !allowed(a.href)) { log('redirect', a.href); return; }
      return orig.call(this);
    });

    // 3. Synthetic click events dispatched at links
    patchMethod(w.EventTarget && w.EventTarget.prototype, 'dispatchEvent', (orig) => function dispatchEvent(evt) {
      if (evt && /^(click|auxclick|mouseup|pointerup|touchend)$/.test(evt.type)) {
        const a = linkOf(this);
        if (a && !allowed(a.href)) { log('redirect', a.href); return false; }
      }
      return orig.call(this, evt);
    });

    // 4. Off-site form submissions
    const formOk = (f) => !f.action || allowed(f.action);
    patchMethod(w.HTMLFormElement && w.HTMLFormElement.prototype, 'submit', (orig) => function submit() {
      if (!formOk(this)) return log('redirect', this.action);
      return orig.call(this);
    });
    patchMethod(w.HTMLFormElement && w.HTMLFormElement.prototype, 'requestSubmit', (orig) => function requestSubmit(...a) {
      if (!formOk(this)) return log('redirect', this.action);
      return orig.apply(this, a);
    });

    // 5. Refuse ad-network scripts / frames before they load
    for (const C of ['HTMLScriptElement', 'HTMLIFrameElement']) {
      patchSetter(w[C] && w[C].prototype, 'src', (set) => function (v) {
        if (isAdUrl(v)) { log('ad script', v); return; }
        return set.call(this, v);
      });
    }
    patchMethod(w.Element && w.Element.prototype, 'setAttribute', (orig) => function setAttribute(name, value) {
      if (typeof name === 'string' && name.toLowerCase() === 'src' &&
          (this.tagName === 'SCRIPT' || this.tagName === 'IFRAME') && isAdUrl(value)) {
        log('ad script', value);
        return;
      }
      return orig.call(this, name, value);
    });

    // 6. Guard any same-origin frame the moment someone reaches into it
    const guardChild = (get) => function () {
      const r = get.call(this);
      try { guardRealm(r && r.defaultView ? r.defaultView : r); } catch (_) {}
      return r;
    };
    for (const C of ['HTMLIFrameElement', 'HTMLFrameElement', 'HTMLObjectElement']) {
      patchGetter(w[C] && w[C].prototype, 'contentWindow', guardChild);
      patchGetter(w[C] && w[C].prototype, 'contentDocument', guardChild);
    }
  }

  guardRealm(window);

  // ---- Real taps on off-site links -----------------------------------------
  function onTap(e) {
    const a = linkOf(e.target);
    if (a && !allowed(a.href)) {
      e.preventDefault();
      e.stopImmediatePropagation();
      log('link', a.href);
    }
  }
  window.addEventListener('click', onTap, true);
  window.addEventListener('auxclick', onTap, true);

  window.addEventListener('submit', (e) => {
    const f = e.target;
    const action = (e.submitter && e.submitter.formAction) || (f && f.action);
    if (action && !allowed(action)) {
      e.preventDefault();
      e.stopImmediatePropagation();
      log('redirect', action);
    }
  }, true);

  // ---- location.href = '...' redirects (iOS/Safari with Navigation API) ---
  if (window.navigation && typeof navigation.addEventListener === 'function') {
    navigation.addEventListener('navigate', (e) => {
      if (e.cancelable && !allowed(e.destination.url)) {
        e.preventDefault();
        log('redirect', e.destination.url);
      }
    });
  }

  // ---- Sandbox embedded players -------------------------------------------
  // Without allow-popups / allow-top-navigation the player can't open new
  // tabs or send this tab elsewhere, no matter what its ad scripts do.
  const SANDBOX = 'allow-scripts allow-same-origin allow-forms allow-presentation ' +
                  'allow-pointer-lock allow-orientation-lock allow-downloads';

  function sandboxFrame(f) {
    if (!SANDBOX_PLAYERS || f.hasAttribute('sandbox')) return;
    const src = f.getAttribute('src') || '';
    if (!/^(https?:)?\/\//i.test(src)) return; // about:blank etc. are handled by guardRealm
    f.setAttribute('sandbox', SANDBOX);
    // Sandbox flags only apply to the next load, so reload the frame once.
    if (f.isConnected) f.setAttribute('src', src);
  }

  // ---- Overlays, meta refreshes, ad tags in the HTML ----------------------
  const isTransparent = (c) => c === 'transparent' || /rgba\([^)]*,\s*0\)$/.test(c);

  function isOverlay(el) {
    if (!REMOVE_OVERLAYS || el.id === '__guard_toast' || el === document.body || el === document.documentElement) return false;
    const r = el.getBoundingClientRect();
    const big = r.width * r.height;
    const screenArea = innerWidth * innerHeight;

    // A huge link to another site = click-jacking overlay
    if (el.tagName === 'A' && el.href && !allowed(el.href) && big > screenArea * 0.25) return true;

    if (big < screenArea * 0.5) return false;
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' && cs.position !== 'absolute') return false;
    if (cs.pointerEvents === 'none' || cs.display === 'none' || cs.visibility === 'hidden') return false;
    if ((parseInt(cs.zIndex, 10) || 0) < 1000) return false;
    if (/^(VIDEO|IFRAME)$/.test(el.tagName) || el.querySelector('video,iframe')) return false;
    if (parseFloat(cs.opacity) < 0.1) return true;
    return isTransparent(cs.backgroundColor) && cs.backgroundImage === 'none' &&
           !el.textContent.trim() && !el.querySelector('img,svg,canvas,picture,button,input');
  }

  function handleNode(n) {
    if (n.nodeType !== 1) return;
    const tag = n.tagName;
    if (tag === 'SCRIPT') {
      if (n.src && isAdUrl(n.src)) { n.type = 'javascript/blocked'; n.remove(); log('ad script', n.src); }
      return;
    }
    if (tag === 'IFRAME') {
      if (n.src && isAdUrl(n.src)) { n.remove(); log('ad frame', n.src); return; }
      try { guardRealm(n.contentWindow); } catch (_) {}
      sandboxFrame(n);
      return;
    }
    if (tag === 'META' && /refresh/i.test(n.httpEquiv)) {
      const url = ((n.content || '').split(/url=/i)[1] || '').trim().replace(/^['"]|['"]$/g, '');
      if (url && !allowed(url)) { n.remove(); log('redirect', url); }
      return;
    }
    if (n.querySelectorAll) {
      for (const c of n.querySelectorAll('script[src],iframe,meta[http-equiv]')) handleNode(c);
    }
    const top = n.parentNode === document.body || n.parentNode === document.documentElement;
    if (document.body && n.isConnected && (top || tag === 'A') && isOverlay(n)) { n.remove(); log('overlay', n.href || n.tagName.toLowerCase()); }
  }

  new MutationObserver((muts) => {
    for (const m of muts) for (const n of m.addedNodes) handleNode(n);
  }).observe(document, { childList: true, subtree: true });

  // Overlays are sometimes made visible / resized after insertion, so do a
  // light periodic check of the page's top-level layers and big links.
  setInterval(() => {
    if (!document.body) return;
    for (const el of document.body.children) if (isOverlay(el)) { el.remove(); log('overlay', el.tagName.toLowerCase()); }
    for (const a of document.querySelectorAll('a[href]')) if (isOverlay(a)) { a.remove(); log('overlay', a.href); }
  }, 1500);
})();
