# -*- coding: utf-8 -*-
"""
采集器 — 把"活页面"变成一份给 LLM / 规划器看的元素清单 (JSON, 不是 HTML)

设计取舍:
- **一次 JS 往返**拿全量: 逐个 find_element 在几百个元素上会慢到不可用, 也让采集
  变成"多次读取之间页面可能变了"的竞态。
- **只读**: 只 execute_script + 属性读写, 不 click / 不 send_keys / 不提交表单。
  唯一写 DOM 的动作是打 `data-ai-eid` 标记 (opt.markEid=False 可关), 它让 eid 能
  用 `[data-ai-eid="e12"]` 稳定找回, 比"再算一遍 nth-child"可靠。
- **不采表单值**: input/textarea 的 value 一律不采 (只记 has_value), outerHTML 里
  的 value 也替换成 [REDACTED] —— 采集结果要进 LLM, 不能顺手把用户数据带走。
- **裁剪即降本**: 属性白名单 + 重复行折叠 + 超量按容器分组 + 单元素 HTML 截断,
  都是为了让 token 花在结构上而不是样式噪音上。
- **降级不抛**: 没有 driver / JS 报错 / 返回脏数据 → 返回空清单 + notes, 链路继续。

JS 侧的两个坑 (老 DOM_SCRAPE_JS 全踩了, 这里必须避开):
1. selenium 把脚本当**函数体**执行, 所以整段必须是一个带 `return` 的表达式 ——
   `(function(){...})();` 这种语句形式的 IIFE 永远返回 undefined。
2. 脚本开头不能是 `//` 注释: `"return " + 脚本` 会退化成 `return;` (ASI)。

对外:
    collect(driver, profile=None) -> dict      # 一份完整 inventory
    COLLECT_JS                                 # 纯表达式, 用 "return " + COLLECT_JS 执行
    empty_inventory()                          # 降级用的空骨架 (键齐全)
"""
from __future__ import annotations

import json
import warnings
from typing import Any, Dict, List, Mapping, Optional

from ai_kit.profiles import effective_rules

COLLECTOR_VERSION = 1

DEFAULT_MAX_ELEMENTS = 400        # 交互元素上限, 超了按容器分组摘要
DEFAULT_MAX_HTML = 2000           # 单元素 outerHTML 截断长度

# 定位属性优先级 (data-testid 最稳, 其次是团队自定的 data-cy/data-qa 之类)。
# 稳定度打分表在 JS 里: testid/id=1.0, aria-label=0.9, name=0.85, 语义 css=0.7,
# 相对 xpath / text=0.4。id 额外分档 —— `el-id-1234-5` / `:r3:` 这种框架生成的 id
# 每次渲染都变, 给 1.0 会让规划器优先用它, 属于"看起来最稳其实最脆"的陷阱。
LOCATOR_ATTRS = ("data-testid", "data-test", "data-cy", "data-qa", "data-e2e")


# ---------------------------------------------------------------------------
# 采集 JS (纯表达式, 不是语句)
# ---------------------------------------------------------------------------

COLLECT_JS = r"""(function (opt) {
  opt = opt || {};
  var MAX_EL = opt.maxElements || 400;
  var MAX_HTML = opt.maxHtml || 2000;
  var MARK = opt.markEid !== false;
  var EID_ATTR = 'data-ai-eid';
  var RISK = opt.riskKeywords || {};
  var HINTS = opt.containerHints || {};
  var LOC_ATTRS = opt.locatorAttrs || ['data-testid', 'data-test', 'data-cy', 'data-qa', 'data-e2e'];
  var VERSION = opt.version || 1;
  var notes = [];

  var SKIP_TAGS = {SCRIPT: 1, STYLE: 1, SVG: 1, NOSCRIPT: 1, TEMPLATE: 1,
                   LINK: 1, META: 1, HEAD: 1, TITLE: 1};
  var ATTR_OK = {id: 1, name: 1, 'class': 1, type: 1, placeholder: 1, role: 1, title: 1,
                 href: 1, alt: 1, maxlength: 1, min: 1, max: 1, step: 1, pattern: 1,
                 required: 1, readonly: 1, disabled: 1, 'for': 1, colspan: 1, rowspan: 1,
                 contenteditable: 1, autocomplete: 1, multiple: 1, checked: 1, selected: 1,
                 rows: 1, cols: 1, tabindex: 1, src: 1};
  var CLASS_RE = /^[A-Za-z_][\w-]*$/;
  var STATE_RE = /^(is-|has-|active$|focus|hover|checked$|selected$|disabled$|open$|show$|collapsed$|expanded$|loading$)/;
  var DYN_ID_RE = /^:r|^el-id-|[0-9a-f]{8}-[0-9a-f]{4}-|\d{3,}/i;
  var RISK_ORDER = ['destructive', 'write'];

  function note(msg) { if (notes.indexOf(msg) < 0) notes.push(msg); }
  function txt(el, n) {
    var t = String(el.textContent || '').replace(/\s+/g, ' ').replace(/^ | $/g, '');
    return t.length > n ? t.slice(0, n) : t;
  }
  function vis(el) {
    try {
      if (!el.getClientRects().length) return false;
      var st = window.getComputedStyle(el);
      if (!st) return false;
      return st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
    } catch (e) { return false; }
  }
  function insideSkipped(el) {
    var n = el;
    while (n && n.nodeType === 1) {
      if (SKIP_TAGS[n.tagName]) return true;
      n = n.parentElement;
    }
    return false;
  }
  function wanted(n) {
    return ATTR_OK[n] === 1 || n.indexOf('aria-') === 0 || n.indexOf('data-test') === 0 ||
           n.indexOf('data-cy') === 0 || n.indexOf('data-qa') === 0 || n.indexOf('data-e2e') === 0;
  }
  function attrMap(el, tag) {
    var out = {}, i, a, n, v;
    for (i = 0; i < el.attributes.length; i++) {
      a = el.attributes[i];
      n = String(a.name).toLowerCase();
      if (!wanted(n)) continue;
      if (n === 'value') continue;                     // 表单值一律不采 (PII)
      v = String(a.value);
      if (n === 'class') v = v.split(/\s+/).slice(0, 8).join(' ');
      out[n] = v.length > 120 ? v.slice(0, 120) : v;
    }
    return out;
  }
  function ariaOf(a) {
    var out = {}, k;
    for (k in a) { if (a.hasOwnProperty(k) && k.indexOf('aria-') === 0) out[k.slice(5)] = a[k]; }
    return out;
  }
  function cssStr(v) { return String(v).replace(/(["\\])/g, '\\$1'); }
  function xpathStr(v) {
    if (v.indexOf('"') < 0) return '"' + v + '"';
    if (v.indexOf("'") < 0) return "'" + v + "'";
    return '';
  }
  function semanticCss(el, tag) {
    var cls = String(el.getAttribute('class') || '').split(/\s+/);
    var keep = [], i, c;
    for (i = 0; i < cls.length && keep.length < 2; i++) {
      c = cls[i];
      if (!c || !CLASS_RE.test(c) || STATE_RE.test(c)) continue;
      keep.push(c);
    }
    if (!keep.length) return '';
    var out = tag;
    for (i = 0; i < keep.length; i++) out += '.' + keep[i];
    return out;
  }
  function relXPath(el) {
    var parts = [], node = el, hops = 0, id;
    while (node && node.nodeType === 1 && hops < 4) {
      id = node.getAttribute('id') || '';
      if (id && CLASS_RE.test(id) && !DYN_ID_RE.test(id)) {
        parts.unshift('//*[@id="' + cssStr(id) + '"]');
        return parts.join('/');
      }
      var tag = node.tagName.toLowerCase();
      var i = 1, sib = node.previousElementSibling;
      while (sib) { if (sib.tagName === node.tagName) { i++; } sib = sib.previousElementSibling; }
      parts.unshift(tag + '[' + i + ']');
      node = node.parentElement;
      hops++;
    }
    return parts.length ? '//' + parts.join('/') : '';
  }
  function locatorsOf(el, tag, a) {
    var out = [], i, n, v;
    for (i = 0; i < LOC_ATTRS.length; i++) {
      n = LOC_ATTRS[i];
      if (a[n]) {
        out.push({by: 'css selector', value: '[' + n + '="' + cssStr(a[n]) + '"]',
                  stability: 1.0, strategy: n});
        break;
      }
    }
    var id = a.id;
    if (id) {
      if (CLASS_RE.test(id)) {
        var dyn = DYN_ID_RE.test(id);
        out.push({by: 'css selector', value: '#' + id,
                  stability: dyn ? 0.6 : 1.0, strategy: dyn ? 'id(疑似动态)' : 'id'});
      } else {
        out.push({by: 'css selector', value: '[id="' + cssStr(id) + '"]',
                  stability: 0.6, strategy: 'id(需转义)'});
      }
    }
    if (a['aria-label']) {
      out.push({by: 'css selector', value: '[aria-label="' + cssStr(a['aria-label']) + '"]',
                stability: 0.9, strategy: 'aria-label'});
    }
    if (a.name) {
      out.push({by: 'css selector', value: '[name="' + cssStr(a.name) + '"]',
                stability: 0.85, strategy: 'name'});
    }
    var sem = semanticCss(el, tag);
    if (sem) out.push({by: 'css selector', value: sem, stability: 0.7, strategy: 'semantic-css'});
    var xp = relXPath(el);
    if (xp) out.push({by: 'xpath', value: xp, stability: 0.4, strategy: 'relative-xpath'});
    var t = txt(el, 30);
    if (t && /^(button|a|span|label|li|td|div|i)$/.test(tag)) {
      var quoted = xpathStr(t);
      if (quoted) {
        out.push({by: 'xpath', value: '//' + tag + '[normalize-space()=' + quoted + ']',
                  stability: 0.4, strategy: 'text'});
      }
    }
    return out;
  }
  function implicitRole(tag, type) {
    if (tag === 'button') return 'button';
    if (tag === 'a') return 'link';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'submit' || type === 'button' || type === 'reset') return 'button';
      if (type === 'search') return 'searchbox';
      return 'textbox';
    }
    return '';
  }
  function kindOf(tag, a, type, role, cls) {
    if (tag === 'select' || role === 'combobox' || role === 'listbox' ||
        cls.indexOf(' el-select ') >= 0 || cls.indexOf(' ant-select ') >= 0) return 'select';
    if (tag === 'input' || tag === 'textarea' || role === 'textbox' ||
        role === 'searchbox' || a.contenteditable === 'true') return 'input';
    if (tag === 'button' || tag === 'a' || role === 'button' || role === 'menuitem' ||
        role === 'tab' || role === 'switch' || type === 'submit' || type === 'button' ||
        type === 'reset' || cls.indexOf('btn') >= 0 || cls.indexOf('button') >= 0) return 'button';
    return '';
  }
  function actionsOf(kind, type) {
    if (kind === 'select') return ['select'];
    if (kind === 'input') {
      if (type === 'checkbox' || type === 'radio') return ['check', 'uncheck'];
      return ['fill', 'clear'];
    }
    if (kind === 'button') return ['click'];
    return [];
  }
  function closestSel(el, sels) {
    if (!sels || !sels.length) return '';
    var host = el.parentElement || el;
    for (var i = 0; i < sels.length; i++) {
      var s = sels[i];
      if (!s) continue;
      try { if (host.closest(s)) return s; }
      catch (e) { note('容器选择器无效: ' + s); }
    }
    return '';
  }
  function hint(kind) {
    var v = HINTS[kind];
    if (v && v.length) return v;
    return kind === 'dialog' ? ['[role=dialog]', '.modal', '[class*=dialog]'] : [];
  }
  function containerOf(el) {
    if (el.closest('tr, [role=row], .el-table__row, .ant-table-row')) return 'table-row';
    if (closestSel(el, hint('dialog'))) return 'dialog';
    if (closestSel(el, hint('filter-bar'))) return 'filter-bar';
    if (el.closest('form, [role=form], .el-form, .ant-form')) return 'form';
    if (closestSel(el, hint('toolbar'))) return 'toolbar';
    return 'page';
  }
  function riskOf(a, text) {
    var hay = (text + ' ' + (a['aria-label'] || '') + ' ' + (a.title || '')).toLowerCase();
    var levels = RISK_ORDER.slice(), k, i, j, words, w;
    for (k in RISK) { if (RISK.hasOwnProperty(k) && levels.indexOf(k) < 0) levels.push(k); }
    for (i = 0; i < levels.length; i++) {
      words = RISK[levels[i]];
      if (!words || !words.length) continue;
      for (j = 0; j < words.length; j++) {
        w = String(words[j]).toLowerCase();
        if (w && hay.indexOf(w) >= 0) return levels[i];
      }
    }
    return null;
  }
  function constraintsOf(el, tag, a, type) {
    var c = {};
    if (tag !== 'input' && tag !== 'textarea' && tag !== 'select') return c;
    c.required = !!(el.required || a['aria-required'] === 'true');
    c.type = type || tag;
    c.readonly = !!(el.readOnly || a.readonly !== undefined);
    c.disabled = !!(el.disabled || a['aria-disabled'] === 'true');
    if (el.maxLength && el.maxLength > 0 && el.maxLength < 100000) c.maxlength = el.maxLength;
    if (a.min !== undefined) c.min = a.min;
    if (a.max !== undefined) c.max = a.max;
    if (a.step !== undefined) c.step = a.step;
    if (a.pattern) c.pattern = a.pattern;
    c.has_value = String(el.value === undefined || el.value === null ? '' : el.value).length > 0;
    return c;
  }
  function outerHtml(el, tag) {
    var h = String(el.outerHTML || '');
    // 自己打的 eid 标记要抹掉: 它是采集器的东西, 不该污染给 LLM 看的 DOM,
    // 否则模型很可能把 [data-ai-eid=e3] 当成一个真实可用的定位串拾起来。
    h = h.replace(/\sdata-ai-eid="[^"]*"/i, '');
    if (tag === 'input' || tag === 'textarea') {
      h = h.replace(/\svalue\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/i, ' value="[REDACTED]"');
    }
    return h.length > MAX_HTML ? h.slice(0, MAX_HTML) + '...[TRUNCATED]' : h;
  }
  function optionsOf(el) {
    var out = [], i, o = el.querySelectorAll('option, [role=option], .el-select-dropdown__item');
    for (i = 0; i < o.length && out.length < 20; i++) {
      var t = txt(o[i], 30);
      if (t) out.push(t);
    }
    return out;
  }

  // 旧采集留下的标记会让 eid 指向错的元素, 先清干净再重新编号
  var stale = document.querySelectorAll('[' + EID_ATTR + ']');
  for (var s0 = 0; s0 < stale.length; s0++) { stale[s0].removeAttribute(EID_ATTR); }

  var SEL = 'input, textarea, select, button, a[href], [role=button], [role=menuitem],' +
            '[role=tab], [role=switch], [role=checkbox], [role=radio], [role=combobox],' +
            '[role=searchbox], [role=textbox], [contenteditable=true], .el-button,' +
            '.el-input__inner, .el-select, .ant-btn, .ant-select, input[type=file]';
  var nodes = document.querySelectorAll(SEL);
  var items = [], discovered = 0, i, el, tag, a, type, role, cls, kd, eid;

  for (i = 0; i < nodes.length; i++) {
    el = nodes[i];
    if (!el || el.nodeType !== 1) continue;
    tag = el.tagName.toLowerCase();
    if (SKIP_TAGS[el.tagName] || insideSkipped(el)) continue;
    a = attrMap(el, tag);
    type = String(a.type || '').toLowerCase();
    role = String(a.role || '').toLowerCase();
    cls = ' ' + String(a['class'] || '').toLowerCase() + ' ';
    kd = kindOf(tag, a, type, role, cls);
    if (!kd) continue;
    if (kd === 'input') {
      var wrap = el.closest('.el-select, .ant-select');
      if (wrap && wrap !== el) continue;              // 已由外层 select 代表, 不重复计
    }
    if (!vis(el)) continue;
    discovered++;
    eid = 'e' + discovered;
    if (MARK) el.setAttribute(EID_ATTR, eid);

    var locs = locatorsOf(el, tag, a);
    var item = {
      eid: eid,
      kind: kd,
      tag: tag,
      type: type,
      role: role || implicitRole(tag, type),
      text: txt(el, 40),
      name: a.name || '',
      attrs: a,
      aria: ariaOf(a),
      container: containerOf(el),
      locator: locs.length ? locs[0] : null,
      alternatives: locs.slice(1),
      constraints: constraintsOf(el, tag, a, type),
      actions: actionsOf(kd, type),
      risk: riskOf(a, txt(el, 60)),
      html: outerHtml(el, tag)
    };
    if (kd === 'select') item.options = optionsOf(el);
    if (!item.locator) note('元素没有可用定位串: ' + tag + ' ' + item.text.slice(0, 20));
    items.push(item);
  }

  // 重复行折叠: 同一套行内按钮在每行重复出现, 每种保留 2 个, 其余只计数
  var rowSeen = {}, kept = [], folded = 0;
  for (i = 0; i < items.length; i++) {
    var it = items[i];
    if (it.container === 'table-row') {
      var rk = it.text + '|' + (it.attrs['class'] || '');
      rowSeen[rk] = (rowSeen[rk] || 0) + 1;
      if (rowSeen[rk] > 2) { folded++; continue; }
    }
    kept.push(it);
  }

  // 超量: 先保非表格行的元素 (表格行已有 row_actions 摘要), 再给分组统计
  var truncated = false, groups = [];
  if (kept.length > MAX_EL) {
    truncated = true;
    var prio = [], rows = [];
    for (i = 0; i < kept.length; i++) {
      (kept[i].container === 'table-row' ? rows : prio).push(kept[i]);
    }
    var capped = prio.slice(0, MAX_EL);
    if (capped.length < MAX_EL) {
      capped = capped.concat(rows.slice(0, MAX_EL - capped.length));
    }
    kept = capped;

    var tally = {}, order = [];
    for (i = 0; i < items.length; i++) {
      var cc = items[i].container;
      if (!tally[cc]) { tally[cc] = {container: cc, count: 0, samples: []}; order.push(cc); }
      tally[cc].count++;
      if (tally[cc].samples.length < 3) {
        tally[cc].samples.push({eid: items[i].eid, text: items[i].text,
                                locator: items[i].locator});
      }
    }
    for (i = 0; i < order.length; i++) groups.push(tally[order[i]]);
    note('交互元素 ' + items.length + ' 个超过上限 ' + MAX_EL +
         ', 已按容器分组截断; 行内按钮请看 tables[].row_actions');
  }

  var buckets = {input: [], button: [], select: []};
  for (i = 0; i < kept.length; i++) {
    if (buckets[kept[i].kind]) buckets[kept[i].kind].push(kept[i]);
  }
  // 行内按钮的 eid 只有在它本身也留在清单里时才能给规划器用 (被折叠/超预算的就不给)
  var keptEid = {};
  for (i = 0; i < kept.length; i++) keptEid[kept[i].eid] = 1;

  // 表格: 列名 + 行数 + 前 2 行样本 + 行内操作按钮 (CRUD 用例的抓手)
  var tNodes = document.querySelectorAll('table, .el-table, .ant-table, [role=table], [role=grid]');
  var tables = [], seenT = [], tn, ttag, tattrs, tc = 0;

  function rowActionsOf(t) {
    var acts = [], map = {}, rows = t.querySelectorAll('tbody tr, [role=row], .el-table__row');
    var r, b, bs, bt, ak, row;
    for (r = 0; r < rows.length; r++) {
      row = rows[r];
      if (!vis(row)) continue;
      bs = row.querySelectorAll('button, a[href], [role=button], .el-button');
      for (b = 0; b < bs.length; b++) {
        bt = txt(bs[b], 20) || bs[b].getAttribute('aria-label') || '';
        if (!bt) continue;
        ak = bt + '|' + (bs[b].getAttribute('class') || '');
        if (!map[ak]) {
          var marked = bs[b].getAttribute(EID_ATTR);
          map[ak] = {text: bt, count: 0, eid: keptEid[marked] ? marked : null,
                     locator: locatorsOf(bs[b], bs[b].tagName.toLowerCase(),
                                         attrMap(bs[b], bs[b].tagName.toLowerCase()))[0] || null};
          acts.push(map[ak]);
        }
        map[ak].count++;
      }
    }
    return acts;
  }

  for (i = 0; i < tNodes.length; i++) {
    tn = tNodes[i];
    if (!tn || seenT.indexOf(tn) >= 0) continue;
    var nested = false;
    for (var j = 0; j < seenT.length; j++) {
      if (seenT[j] !== tn && seenT[j].contains(tn)) { nested = true; break; }
    }
    if (nested) { seenT.push(tn); continue; }
    seenT.push(tn);
    if (!vis(tn)) continue;
    tc++;
    ttag = tn.tagName.toLowerCase();
    tattrs = attrMap(tn, ttag);
    var cols = [], ths = tn.querySelectorAll('thead th, [role=columnheader], .el-table__header th');
    if (!ths.length) ths = tn.querySelectorAll('th');
    for (var q = 0; q < ths.length && cols.length < 30; q++) {
      var ct = txt(ths[q], 30);
      if (ct && cols[cols.length - 1] !== ct) cols.push(ct);
    }
    var rowsAll = tn.querySelectorAll('tbody tr, [role=row], .el-table__row');
    var liveRows = 0, samples = [], rr, cc2, cells, rowTexts;
    for (rr = 0; rr < rowsAll.length; rr++) {
      if (!vis(rowsAll[rr])) continue;
      liveRows++;
      if (samples.length < 2) {
        cells = rowsAll[rr].querySelectorAll('td, [role=cell]');
        rowTexts = [];
        for (cc2 = 0; cc2 < cells.length && cc2 < 20; cc2++) rowTexts.push(txt(cells[cc2], 40));
        samples.push(rowTexts);
      }
    }
    var teid = 'c' + tc;
    if (MARK) tn.setAttribute(EID_ATTR, teid);
    var tlocs = locatorsOf(tn, ttag, tattrs);
    tables.push({
      eid: teid,
      kind: 'table',
      tag: ttag,
      type: '',
      role: String(tattrs.role || 'table'),
      text: txt(tn, 40),
      name: tattrs.name || '',
      attrs: tattrs,
      aria: ariaOf(tattrs),
      container: containerOf(tn),
      locator: tlocs.length ? tlocs[0] : null,
      alternatives: tlocs.slice(1),
      constraints: {},
      actions: [],
      // 表格本身不是可点动作, 不做风险标记 —— 否则“删除时间”这种列名会把整张表标成
      // destructive, 规划器就不敢碰它了。风险只打在可交互元素上。
      risk: null,
      columns: cols,
      row_count: liveRows,
      sample_rows: samples,
      row_actions: rowActionsOf(tn),
      html: outerHtml(tn, ttag)
    });
  }

  // 可见弹窗: 原型分类和"弹窗页"策略都要靠它。
  // 不同选择器会命中同一棵子树的外层/内层 (.el-dialog__wrapper > .el-dialog), 所以
  // 用一个包含判据去重 —— 否则同一个弹窗会被报两次。
  var dialogs = [], dEls = [], dsel = hint('dialog'), d, found, cand, f;
  function seenDialog(node) {
    for (var z = 0; z < dEls.length; z++) {
      if (dEls[z] === node || dEls[z].contains(node) || node.contains(dEls[z])) return true;
    }
    return false;
  }
  for (d = 0; d < dsel.length && dialogs.length < 3; d++) {
    found = null;
    try {
      cand = document.querySelectorAll(dsel[d]);
      for (f = 0; f < cand.length; f++) {
        if (vis(cand[f]) && !seenDialog(cand[f])) { found = cand[f]; break; }
      }
    } catch (e) { note('容器选择器无效: ' + dsel[d]); continue; }
    if (!found) continue;
    var deid = 'd' + (dialogs.length + 1);
    if (MARK) found.setAttribute(EID_ATTR, deid);
    dEls.push(found);
    var dbtns = [], db = found.querySelectorAll('button, [role=button], .el-button');
    for (f = 0; f < db.length && dbtns.length < 8; f++) {
      var dt = txt(db[f], 20);
      if (dt) dbtns.push(dt);
    }
    dialogs.push({eid: deid, selector: dsel[d], text: txt(found, 120), buttons: dbtns});
  }

  var summary = {}, risks = {};
  for (i = 0; i < items.length; i++) {
    summary[items[i].container] = (summary[items[i].container] || 0) + 1;
    if (items[i].risk) risks[items[i].risk] = (risks[items[i].risk] || 0) + 1;
  }

  return JSON.stringify({
    collector_version: VERSION,
    url: location.href,
    title: document.title,
    ready_state: document.readyState,
    viewport: {width: window.innerWidth, height: window.innerHeight},
    counts: {
      discovered: discovered,
      interactive: items.length,
      kept: kept.length,
      folded_rows: folded,
      inputs: buckets.input.length,
      buttons: buckets.button.length,
      selects: buckets.select.length,
      tables: tables.length
    },
    container_summary: summary,
    dialogs: dialogs,
    risks: risks,
    truncated: truncated,
    groups: groups,
    notes: notes,
    inputs: buckets.input,
    buttons: buckets.button,
    selects: buckets.select,
    tables: tables
  });
})(arguments[0])
"""


# ---------------------------------------------------------------------------
# Python 侧: 降级骨架 + 归一化 (脏数据不进下游)
# ---------------------------------------------------------------------------

def empty_inventory(page_name: str = "", reason: str = "") -> Dict[str, Any]:
    """键齐全的空清单 —— 采集失败时下游拿到的形状不变, 不用到处判 None"""
    inv: Dict[str, Any] = {
        "collector_version": COLLECTOR_VERSION,
        "page_name": page_name,
        "url": "",
        "title": "",
        "ready_state": "",
        "viewport": {"width": 0, "height": 0},
        "counts": {"discovered": 0, "interactive": 0, "kept": 0, "folded_rows": 0,
                   "inputs": 0, "buttons": 0, "selects": 0, "tables": 0},
        "container_summary": {},
        "dialogs": [],
        "risks": {},
        "truncated": False,
        "groups": [],
        "notes": [],
        "inputs": [],
        "buttons": [],
        "selects": [],
        "tables": [],
    }
    if reason:
        inv["notes"].append(reason)
    return inv


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_text(value: Any, limit: int = 0) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text[:limit] if limit and len(text) > limit else text


def _clean_locator(raw: Any) -> Optional[Dict[str, Any]]:
    """{by, value, stability, strategy} —— 缺 by/value 的候选直接丢, 不能进下游"""
    if not isinstance(raw, Mapping):
        return None
    by, value = _as_text(raw.get("by")), _as_text(raw.get("value"))
    if not by or not value:
        return None
    try:
        stability = float(raw.get("stability", 0.0))
    except (TypeError, ValueError):
        stability = 0.0
    return {
        "by": by,
        "value": value,
        "stability": round(max(0.0, min(1.0, stability)), 2),
        "strategy": _as_text(raw.get("strategy")),
    }


def _clean_element(raw: Any, kind: str = "") -> Optional[Dict[str, Any]]:
    """单个元素归一: 没有 eid 的一律丢弃 (定位不到的东西下游也用不了)"""
    if not isinstance(raw, Mapping):
        return None
    eid = _as_text(raw.get("eid"))
    if not eid:
        return None

    locator = _clean_locator(raw.get("locator"))
    alternatives = [c for c in (_clean_locator(x) for x in _as_list(raw.get("alternatives"))) if c]
    if locator is None and alternatives:      # locator 写坏就用第一个备选顶上
        locator, alternatives = alternatives[0], alternatives[1:]

    attrs = _as_dict(raw.get("attrs"))
    element_kind = _as_text(raw.get("kind")) or kind
    if element_kind == "input":
        # 双保险: JS 已经不采 value, 这里再拦一次 (脏 payload / 老的采集结果)
        attrs.pop("value", None)

    risk = _as_text(raw.get("risk"))
    element = {
        "eid": eid,
        "kind": element_kind,
        "tag": _as_text(raw.get("tag")),
        "type": _as_text(raw.get("type")),
        "role": _as_text(raw.get("role")),
        "text": _as_text(raw.get("text"), 80),
        "name": _as_text(raw.get("name")),
        "attrs": attrs,
        "aria": _as_dict(raw.get("aria")),
        "container": _as_text(raw.get("container")) or "page",
        "locator": locator,
        "alternatives": alternatives,
        "constraints": _as_dict(raw.get("constraints")),
        "actions": [_as_text(a) for a in _as_list(raw.get("actions")) if _as_text(a)],
        "risk": risk or None,
        "html": _as_text(raw.get("html")),
    }
    options = [_as_text(o, 40) for o in _as_list(raw.get("options")) if _as_text(o)]
    if options:
        element["options"] = options[:20]
    return element


def _clean_table(raw: Any) -> Optional[Dict[str, Any]]:
    base = _clean_element(raw, kind="table")
    if base is None:
        return None
    rows = []
    for row in _as_list(raw.get("sample_rows"))[:2]:
        if isinstance(row, list):
            rows.append([_as_text(c, 60) for c in row][:20])
    actions = []
    for act in _as_list(raw.get("row_actions"))[:20]:
        if not isinstance(act, Mapping):
            continue
        text = _as_text(act.get("text"), 40)
        if not text:
            continue
        actions.append({
            "text": text,
            "eid": _as_text(act.get("eid")) or None,
            "count": _as_int(act.get("count")),
            "locator": _clean_locator(act.get("locator")),
        })
    base.update({
        "kind": "table",
        "columns": [_as_text(c, 40) for c in _as_list(raw.get("columns")) if _as_text(c)],
        "row_count": _as_int(raw.get("row_count")),
        "sample_rows": rows,
        "row_actions": actions,
    })
    return base


def _clean_dialog(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return None
    eid = _as_text(raw.get("eid"))
    if not eid:
        return None
    return {
        "eid": eid,
        "selector": _as_text(raw.get("selector")),
        "text": _as_text(raw.get("text"), 160),
        "buttons": [_as_text(b, 40) for b in _as_list(raw.get("buttons")) if _as_text(b)][:8],
    }


def _clean_elements(raw_items: Any, kind: str, limit: int,
                    seen: set) -> List[Dict[str, Any]]:
    """归一 + 按 eid 去重 + 截断; seen 跨类别共享, 保证 eid 全局唯一"""
    out: List[Dict[str, Any]] = []
    for raw in _as_list(raw_items):
        element = _clean_element(raw, kind=kind)
        if element is None or element["eid"] in seen:
            continue
        seen.add(element["eid"])
        out.append(element)
        if len(out) >= limit:
            break
    return out


def normalize(payload: Any, page_name: str = "",
              max_elements: int = DEFAULT_MAX_ELEMENTS) -> Dict[str, Any]:
    """把 JS 返回的 JSON 归一成稳定结构; 任何脏数据都降级, 不抛"""
    inv = empty_inventory(page_name)
    if not isinstance(payload, Mapping):
        inv["notes"].append(f"采集结果不是 JSON object: {type(payload).__name__}")
        return inv

    inv["url"] = _as_text(payload.get("url"))
    inv["title"] = _as_text(payload.get("title"))
    inv["ready_state"] = _as_text(payload.get("ready_state"))
    viewport = _as_dict(payload.get("viewport"))
    inv["viewport"] = {"width": _as_int(viewport.get("width")),
                       "height": _as_int(viewport.get("height"))}
    inv["truncated"] = bool(payload.get("truncated"))

    seen: set = set()
    inv["inputs"] = _clean_elements(payload.get("inputs"), "input", max_elements, seen)
    inv["buttons"] = _clean_elements(payload.get("buttons"), "button", max_elements, seen)
    inv["selects"] = _clean_elements(payload.get("selects"), "select", max_elements, seen)

    tables: List[Dict[str, Any]] = []
    for raw in _as_list(payload.get("tables")):
        table = _clean_table(raw)
        if table is None or table["eid"] in seen:
            continue
        seen.add(table["eid"])
        tables.append(table)
        if len(tables) >= 20:
            break
    inv["tables"] = tables

    inv["dialogs"] = [d for d in (_clean_dialog(x) for x in _as_list(payload.get("dialogs"))) if d][:3]

    # counts / 分组 / 风险统计一律重算 —— 不信 JS 传上来的数, 只信元素列表本身
    elements = inv["inputs"] + inv["buttons"] + inv["selects"]
    raw_counts = _as_dict(payload.get("counts"))
    inv["counts"] = {
        "discovered": max(_as_int(raw_counts.get("discovered")), len(elements)),
        "interactive": len(elements),
        "kept": len(elements),
        "folded_rows": _as_int(raw_counts.get("folded_rows")),
        "inputs": len(inv["inputs"]),
        "buttons": len(inv["buttons"]),
        "selects": len(inv["selects"]),
        "tables": len(tables),
    }
    summary: Dict[str, int] = {}
    risks: Dict[str, int] = {}
    for element in elements:
        summary[element["container"]] = summary.get(element["container"], 0) + 1
        if element["risk"]:
            risks[element["risk"]] = risks.get(element["risk"], 0) + 1
    inv["container_summary"] = summary
    inv["risks"] = risks

    groups = []
    for raw in _as_list(payload.get("groups"))[:10]:
        if not isinstance(raw, Mapping):
            continue
        name = _as_text(raw.get("container"))
        if not name:
            continue
        groups.append({
            "container": name,
            "count": _as_int(raw.get("count")),
            "samples": [s for s in (_clean_sample(x) for x in _as_list(raw.get("samples"))) if s][:3],
        })
    inv["groups"] = groups

    notes = [_as_text(n, 200) for n in _as_list(payload.get("notes")) if _as_text(n)]
    inv["notes"] = inv["notes"] + notes
    return inv


def _clean_sample(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return None
    eid = _as_text(raw.get("eid"))
    if not eid:
        return None
    return {"eid": eid, "text": _as_text(raw.get("text"), 40),
            "locator": _clean_locator(raw.get("locator"))}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def build_options(profile=None, *,
                  mark_eid: bool = True,
                  max_elements: int = DEFAULT_MAX_ELEMENTS,
                  max_html: int = DEFAULT_MAX_HTML) -> Dict[str, Any]:
    """把规则包 + 采集参数翻译成 JS 的 arguments[0]。

    风险词 / 容器提示来自 profile (内置 generic 打底, 站点包可追加自己的业务词);
    规则包缺失时退化成空 —— 采集照常跑, 只是不打风险标、容器判不准。
    """
    rules = effective_rules(profile)
    risk_keywords = rules.get("risk_keywords") or {}
    container_hints = rules.get("container_hints") or {}
    if not risk_keywords:
        warnings.warn("规则包里没有 risk_keywords, 本次采集不会标记风险动作",
                      RuntimeWarning, stacklevel=2)
    return {
        "version": COLLECTOR_VERSION,
        "markEid": bool(mark_eid),
        "maxElements": max(1, min(_as_int(max_elements, DEFAULT_MAX_ELEMENTS), 5000)),
        "maxHtml": max(200, min(_as_int(max_html, DEFAULT_MAX_HTML), 20000)),
        "locatorAttrs": list(LOCATOR_ATTRS),
        "riskKeywords": risk_keywords,
        "containerHints": container_hints,
    }


def collect(driver, profile=None, *,
            page_name: str = "",
            mark_eid: bool = True,
            max_elements: int = DEFAULT_MAX_ELEMENTS,
            max_html: int = DEFAULT_MAX_HTML) -> Dict[str, Any]:
    """采集当前页面, 返回 inventory dict。

    driver=None / 页面白屏 / JS 报错 / 返回脏数据 → 空清单 + notes, 从不抛异常。
    profile 与 healer 同语义: dict / 规则包路径 / 包名, 不传则只用内置包。
    """
    if driver is None:
        return empty_inventory(page_name, "没有传 driver, 返回空清单")

    options = build_options(profile, mark_eid=mark_eid,
                            max_elements=max_elements, max_html=max_html)
    # strip() 不是洁癖: 常量前面多一个换行, `return` 就会被 ASI 吃掉变成 `return;`,
    # 拿回来的是 None, 而这种失败是完全静默的 (seen 于老 DOM_SCRAPE_JS)。
    script = "return " + COLLECT_JS.strip()
    try:
        raw = driver.execute_script(script, options)
    except Exception as ex:  # noqa: BLE001 — 页面可能已崩/被导航走, 采集不能反过来打断流程
        warnings.warn(f"采集 JS 执行失败, 返回空清单: {ex}", RuntimeWarning, stacklevel=2)
        return empty_inventory(page_name, f"采集 JS 执行失败: {ex}")

    payload: Any = raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as ex:
            warnings.warn(f"采集结果不是合法 JSON, 返回空清单: {ex}",
                          RuntimeWarning, stacklevel=2)
            return empty_inventory(page_name, f"采集结果不是合法 JSON: {ex}")

    return normalize(payload, page_name=page_name,
                     max_elements=options["maxElements"])
