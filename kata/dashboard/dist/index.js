(function () {
  "use strict";
  // Kata dashboard tab. Plain IIFE against the Hermes plugin SDK: React comes from the host.
  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  var React = SDK.React;
  var h = React.createElement;
  var useState = React.useState, useEffect = React.useEffect, useCallback = React.useCallback;
  var API = "/api/plugins/kata";

  function req(path, method, body) {
    var opts = { method: method || "GET" };
    if (body !== undefined) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    return SDK.fetchJSON(API + path, opts);
  }

  function errText(e) {
    if (!e) return "Something went wrong.";
    if (e.detail) return String(e.detail);
    if (e.message) return e.message;
    return String(e);
  }

  var STATUS_LABEL = {
    candidate: "Awaiting review", active: "Active", rejected: "Rejected",
    disabled: "Disabled", stale: "Needs review", suspended: "Suspended"
  };
  var EFFECT_LABEL = { read: "Read only", write: "Local write", external: "External", unknown: "Unknown" };

  function fmtTime(t) {
    if (!t) return "-";
    var d = new Date(t * 1000);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
      d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }

  function showExpr(e) {
    if (!e) return "?";
    switch (e.kind) {
      case "const": return JSON.stringify(e.value);
      case "param": return "{" + e.name + "}";
      case "ref": return "‹step " + (e.step + 1) + (e.path.length ? "." + e.path.join(".") : "") + "›";
      case "concat": return "\"" + e.parts.map(function (p) {
        return p.kind === "const" && typeof p.value === "string" ? p.value : showExpr(p);
      }).join("") + "\"";
      case "obj": return Object.keys(e.fields).map(function (k) { return k + "=" + showExpr(e.fields[k]); }).join(", ");
      case "arr": return "[" + e.items.map(showExpr).join(", ") + "]";
      default: return "?";
    }
  }

  function plain(text) { return String(text || "").replace(/`/g, ""); }

  function Tag(props) {
    return h("span", { className: "kt-tag kt-tag--" + props.tone }, props.children);
  }

  function Stat(props) {
    return h("div", { className: "kt-stat" },
      h("div", { className: "kt-stat__value" }, props.value),
      h("div", { className: "kt-stat__label" }, props.label));
  }

  function Overview(props) {
    var o = props.data;
    if (!o) return null;
    var k = o.katas || {};
    return h("section", { className: "kt-overview", "aria-label": "Summary" },
      h(Stat, { value: o.events.toLocaleString(), label: "tool calls observed" }),
      h(Stat, { value: (k.candidate || 0) + (k.stale || 0), label: "awaiting your review" }),
      h(Stat, { value: k.active || 0, label: "active katas" }),
      h(Stat, { value: o.tool_calls_saved.toLocaleString(), label: "tool calls saved" }),
      h(Stat, { value: o.model_round_trips_saved.toLocaleString(), label: "model round-trips saved" }));
  }

  function KataRow(props) {
    var k = props.kata;
    var tone = k.status === "active" ? "ok" : (k.status === "candidate" || k.status === "stale" ? "attn" : "muted");
    return h("button", {
      type: "button", className: "kt-row" + (props.selected ? " is-selected" : ""),
      onClick: function () { props.onSelect(k.id); }, "aria-pressed": props.selected
    },
      h("div", { className: "kt-row__main" },
        h("div", { className: "kt-row__name" }, k.name),
        h("div", { className: "kt-row__steps" }, k.steps.map(function (s, i) {
          return h(React.Fragment, { key: i }, i > 0 ? h("span", { className: "kt-arrow", "aria-hidden": true }, "→") : null,
            h("code", null, s));
        }))),
      h("div", { className: "kt-row__meta" },
        h(Tag, { tone: tone }, STATUS_LABEL[k.status] || k.status),
        h("span", { className: "kt-row__support" }, k.support + " conversations")));
  }

  function Section(props) {
    return h("section", { className: "kt-section" },
      h("h3", { className: "kt-section__title" }, h("span", { className: "kt-section__num" }, props.num), props.title),
      props.children);
  }

  function Detail(props) {
    var d = props.detail;
    var _n = useState(""), name = _n[0], setName = _n[1];
    var _r = useState(""), reason = _r[0], setReason = _r[1];
    useEffect(function () { setName(""); setReason(""); }, [d && d.kata.id]);
    if (!d) return null;
    var k = d.kata, spec = d.spec, ev = spec.evidence, b = ev.baseline || {}, val = d.validation || {};
    var busy = props.busy;
    var actions = [];
    if (k.status === "candidate" || k.status === "stale") {
      actions.push(h("div", { key: "approve", className: "kt-approve" },
        h("label", { className: "kt-field" },
          h("span", null, "Tool name"),
          h("input", {
            value: name, onChange: function (e) { setName(e.target.value); },
            placeholder: k.name, spellCheck: false, "aria-label": "Tool name"
          })),
        h("button", {
          type: "button", className: "kt-btn kt-btn--primary", disabled: busy || !val.passed,
          onClick: function () { props.act("approve", { name: name.trim() || null }); }
        }, k.status === "stale" ? "Approve new version" : "Approve and register"),
        k.status === "candidate" ? h("button", {
          type: "button", className: "kt-btn", disabled: busy,
          onClick: function () { props.act("reject", { reason: reason }); }
        }, "Reject") : null));
    }
    if (k.status === "active") {
      actions.push(h("button", { key: "dis", type: "button", className: "kt-btn", disabled: busy,
        onClick: function () { props.act("disable", { reason: "" }); } }, "Disable"));
      if (d.versions.filter(function (v) { return v.approved_at && v.version < k.active_version; }).length) {
        actions.push(h("button", { key: "rb", type: "button", className: "kt-btn", disabled: busy,
          onClick: function () { props.act("rollback", {}); } }, "Roll back"));
      }
    }
    if (k.status === "disabled" && k.active_version) {
      actions.push(h("button", { key: "en", type: "button", className: "kt-btn", disabled: busy,
        onClick: function () { props.act("enable"); } }, "Re-enable"));
    }

    return h("article", { className: "kt-detail", "aria-live": "polite" },
      h("header", { className: "kt-detail__head" },
        h("div", null,
          h("div", { className: "kt-eyebrow" }, (STATUS_LABEL[k.status] || k.status) + " · v" + d.latest_version),
          h("h2", { className: "kt-detail__name" }, k.name),
          h("p", { className: "kt-detail__desc" }, plain(spec.description)),
          k.status_reason && (k.status === "stale" || k.status === "suspended") ?
            h("p", { className: "kt-warn" }, k.status_reason) : null),
        h("div", { className: "kt-actions" }, actions)),

      h(Section, { num: "01", title: "Why Kata proposed this" },
        h("dl", { className: "kt-facts" },
          h("div", null, h("dt", null, "Seen in"), h("dd", null, ev.support + " conversations")),
          h("div", null, h("dt", null, "Held-out fit"), h("dd", null, Math.round(ev.fit_rate * 100) + "%")),
          h("div", null, h("dt", null, "Confidence"), h("dd", null, ev.confidence.toFixed(2))),
          h("div", null, h("dt", null, "By hand"), h("dd", null, b.tool_calls + " tool calls" +
            (b.avg_model_round_trips != null ? ", " + b.avg_model_round_trips + " model round-trips" : ""))),
          b.avg_model_tokens ? h("div", null, h("dt", null, "Tokens per run"), h("dd", null, Math.round(b.avg_model_tokens).toLocaleString())) : null,
          h("div", null, h("dt", null, "Last seen"), h("dd", null, fmtTime(ev.last_seen))))),

      h(Section, { num: "02", title: "Steps, in order" },
        h("ol", { className: "kt-steps" }, spec.steps.map(function (s, i) {
          return h("li", { key: i, className: "kt-step" },
            h("div", { className: "kt-step__head" },
              h("code", { className: "kt-step__tool" }, s.tool),
              h(Tag, { tone: s.effect === "read" ? "ok" : (s.effect === "write" ? "muted" : "attn") }, EFFECT_LABEL[s.effect])),
            h("code", { className: "kt-step__args" }, showExpr(s.args)),
            h("div", { className: "kt-step__why" }, plain(s.effect_reason)));
        }))),

      h(Section, { num: "03", title: "Permissions and side effects" },
        h("ul", { className: "kt-list" },
          h("li", null, "May call only: ", spec.steps.map(function (s) { return s.tool; })
            .filter(function (t, i, a) { return a.indexOf(t) === i; }).join(", ")),
          h("li", null, d.requires_approval_per_call
            ? "Has external or unknown effects. Every invocation asks you first, and those steps are never retried."
            : "Read-only or local steps. Read-only steps may retry once on error."),
          h("li", null, "No model calls between steps. Stops at the first failure and returns the trace."))),

      h(Section, { num: "04", title: "Schemas" },
        h("div", { className: "kt-schemas" },
          h("div", null, h("div", { className: "kt-label" }, "Input"), h("pre", null, JSON.stringify(d.input_schema, null, 2))),
          h("div", null, h("div", { className: "kt-label" }, "Output"), h("pre", null, JSON.stringify(spec.output_schema, null, 2))))),

      h(Section, { num: "05", title: "Validation " + (val.passed ? "passed" : "failed") },
        h("ul", { className: "kt-checks" }, (val.checks || []).map(function (c) {
          return h("li", { key: c.name, className: c.passed ? "is-pass" : "is-fail" },
            h("span", { className: "kt-check__mark", "aria-label": c.passed ? "passed" : "failed" }, c.passed ? "✓" : "✕"),
            h("span", { className: "kt-check__name" }, c.name.replace(/_/g, " ")),
            h("span", { className: "kt-check__detail" }, c.detail));
        }))),

      h(Section, { num: "06", title: "Runs" },
        d.runs.length === 0 ? h("p", { className: "kt-muted" }, k.status === "active"
          ? "No runs yet. The agent sees this tool from its next session."
          : "Runs appear here after approval.")
          : h("table", { className: "kt-table" },
            h("thead", null, h("tr", null, ["When", "Version", "Result", "Time", "Steps"].map(function (t) { return h("th", { key: t }, t); }))),
            h("tbody", null, d.runs.map(function (r) {
              return h("tr", { key: r.id },
                h("td", null, fmtTime(r.started_at)), h("td", null, "v" + r.version),
                h("td", null, r.ok ? h(Tag, { tone: "ok" }, "ok") : h(Tag, { tone: "attn" }, "failed at " + ((r.failed_step || 0) + 1))),
                h("td", null, r.duration_ms + " ms"),
                h("td", null, r.trace.steps.map(function (s) { return s.status; }).join(" · ")));
            })))),

      h(Section, { num: "07", title: "History" },
        h("ul", { className: "kt-audit" }, d.audit.map(function (a) {
          return h("li", { key: a.id }, h("span", { className: "kt-audit__when" }, fmtTime(a.at)),
            h("span", { className: "kt-audit__what" }, a.action), h("span", { className: "kt-muted" }, a.actor + (a.detail ? " — " + a.detail : "")));
        }))));
  }

  function KataPage() {
    var _o = useState(null), overview = _o[0], setOverview = _o[1];
    var _k = useState(null), katas = _k[0], setKatas = _k[1];
    var _s = useState(null), selected = _s[0], setSelected = _s[1];
    var _d = useState(null), detail = _d[0], setDetail = _d[1];
    var _e = useState(null), error = _e[0], setError = _e[1];
    var _b = useState(false), busy = _b[0], setBusy = _b[1];
    var _n = useState(null), notice = _n[0], setNotice = _n[1];
    var _f = useState("review"), filter = _f[0], setFilter = _f[1];

    var load = useCallback(function () {
      return Promise.all([req("/overview"), req("/katas")]).then(function (r) {
        setOverview(r[0]); setKatas(r[1].katas); setError(null);
        return r[1].katas;
      }).catch(function (e) { setError(errText(e)); setKatas([]); });
    }, []);

    useEffect(function () {
      load().then(function (list) {
        if (list && list.length && selected == null) {
          var first = list.filter(function (k) { return k.status === "candidate" || k.status === "stale"; })[0] || list[0];
          setSelected(first.id);
        }
      });
    }, []);

    useEffect(function () {
      if (selected == null) { setDetail(null); return; }
      req("/katas/" + selected).then(setDetail).catch(function (e) { setError(errText(e)); });
    }, [selected]);

    function act(action, body) {
      setBusy(true); setNotice(null);
      req("/katas/" + selected + "/" + action, "POST", body || {}).then(function (res) {
        var verb = { approve: "Approved", reject: "Rejected", disable: "Disabled", enable: "Re-enabled", rollback: "Rolled back" }[action];
        setNotice(verb + " " + (res.name || "") + (res.version ? " (v" + res.version + ")" : "") +
          (action === "approve" ? ". The agent can call it from its next session." : "."));
        return load().then(function () { return req("/katas/" + selected); }).then(setDetail);
      }).catch(function (e) { setNotice(errText(e)); }).finally(function () { setBusy(false); });
    }

    function mine() {
      setBusy(true); setNotice(null);
      req("/mine", "POST", {}).then(function (r) {
        setNotice(r.proposed.length ? "New proposals: " + r.proposed.join(", ")
          : "Analysed " + r.events + " tool calls. Nothing new has repeated often enough yet.");
        return load();
      }).catch(function (e) { setNotice(errText(e)); }).finally(function () { setBusy(false); });
    }

    var groups = {
      review: function (k) { return k.status === "candidate" || k.status === "stale"; },
      active: function (k) { return k.status === "active"; },
      other: function (k) { return ["rejected", "disabled", "suspended"].indexOf(k.status) >= 0; }
    };
    var shown = (katas || []).filter(groups[filter]);

    return h("div", { className: "kt-root" },
      h("header", { className: "kt-head" },
        h("div", null,
          h("h1", { className: "kt-title" }, "Kata"),
          h("p", { className: "kt-sub" }, "Routines your agent repeats, compiled into single tools once you approve them.")),
        h("button", { type: "button", className: "kt-btn", onClick: mine, disabled: busy }, busy ? "Working…" : "Analyse history")),
      notice ? h("div", { className: "kt-notice", role: "status" }, notice) : null,
      error ? h("div", { className: "kt-error", role: "alert" }, "Could not reach Kata: " + error) : null,
      h(Overview, { data: overview }),
      katas === null ? h("div", { className: "kt-skeleton", "aria-busy": "true" }, "Loading…") :
        katas.length === 0 ? h("div", { className: "kt-empty" },
          h("h2", null, "Nothing to review yet"),
          h("p", null, "Kata proposes a routine after it has seen the same sequence of tool calls in "
            + "three separate conversations. Keep working normally; proposals land here."),
          h("p", { className: "kt-muted" }, (overview ? overview.events : 0) + " tool calls observed so far.")) :
          h("div", { className: "kt-split" },
            h("nav", { className: "kt-nav", "aria-label": "Katas" },
              h("div", { className: "kt-tabs", role: "tablist" }, [["review", "To review"], ["active", "Active"], ["other", "Other"]].map(function (t) {
                var n = (katas || []).filter(groups[t[0]]).length;
                return h("button", { key: t[0], type: "button", role: "tab", "aria-selected": filter === t[0],
                  className: "kt-tab" + (filter === t[0] ? " is-on" : ""), onClick: function () { setFilter(t[0]); } },
                  t[1], h("span", { className: "kt-tab__n" }, n));
              })),
              shown.length ? shown.map(function (k) {
                return h(KataRow, { key: k.id, kata: k, selected: k.id === selected, onSelect: setSelected });
              }) : h("p", { className: "kt-muted kt-pad" }, "None here.")),
            h(Detail, { detail: detail, act: act, busy: busy })));
  }

  window.__HERMES_PLUGINS__.register("kata", KataPage);
})();
