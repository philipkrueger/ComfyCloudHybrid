import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const SETTING_KEY = "CloudHybrid.ApiKey";
const API_KEY_URL = "https://platform.comfy.org/profile/api-keys";

function toast(severity, summary, detail) {
    try {
        app.extensionManager.toast.add({ severity, summary, detail, life: 8000 });
    } catch (e) {
        console.log(`[ComfyCloudHybrid] ${summary}: ${detail || ""}`);
    }
}

async function postJson(route, body) {
    const resp = await api.fetchApi(route, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
    });
    return resp.json();
}

async function testConnection() {
    toast("info", "Comfy Cloud", "Testing connection …");
    try {
        const result = await postJson("/cloudhybrid/test");
        toast(result.ok ? "success" : "error", "Comfy Cloud", result.message);
    } catch (e) {
        toast("error", "Comfy Cloud", `Connection test failed: ${e}`);
    }
}

async function rescanBlueprints() {
    toast("info", "Comfy Cloud", "Scanning blueprints …");
    try {
        const result = await postJson("/cloudhybrid/rescan");
        const failed = (result.failed || []).length;
        toast(result.restart_required ? "warn" : "success", "Comfy Cloud",
            `${(result.found || []).length} blueprints found` +
            (failed ? `, ${failed} failed` : "") +
            `. ${result.message || ""}`);
    } catch (e) {
        toast("error", "Comfy Cloud", `Rescan failed: ${e}`);
    }
}

function openApiKeyPage() {
    window.open(API_KEY_URL, "_blank", "noopener");
}

// ---------------------------------------------------------------------------
// Right-click "Convert to Cloud API Node"
//
// NOTE: the subgraph-serialisation and node-creation calls below use the live
// ComfyUI / LiteGraph frontend API, which cannot be exercised in the offline
// test suite. The backend (/cloudhybrid/convert) is fully tested; this glue is
// defensive and may need small tweaks against a specific ComfyUI version.
// ---------------------------------------------------------------------------

const GENERIC_NODE_TYPE = "CloudHybrid_RunWorkflow";

function isSubgraphNode(node) {
    if (!node) return false;
    return Boolean(
        node.subgraph ||
        node.isSubgraphNode?.() ||
        node.constructor?.comfyClass === "SubgraphNode" ||
        node.constructor?.name === "SubgraphNode");
}

// Wrap a canvas subgraph instance into the blueprint shape the backend
// converter expects: { nodes:[instance], definitions:{subgraphs:[def]} }.
function subgraphToBlueprint(node) {
    const sub = node.subgraph;
    if (!sub) throw new Error("This node is not a subgraph.");
    const def = typeof sub.serialize === "function" ? sub.serialize() : { ...sub };
    const defId = node.type || def.id || sub.id;
    def.id = defId;
    if (!def.name) def.name = node.title || sub.name || "Subgraph";
    const instance = typeof node.serialize === "function"
        ? node.serialize() : { id: node.id, type: defId };
    instance.type = defId;
    return {
        version: 0.4,
        nodes: [instance],
        links: [],
        definitions: { subgraphs: [def] },
        extra: {},
    };
}

// Modal listing errors (blocking) and warnings (hints) — the "Fehler-Report".
function showReportDialog(title, report) {
    const overlay = document.createElement("div");
    overlay.style.cssText =
        "position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:3000;" +
        "display:flex;align-items:center;justify-content:center;";
    const box = document.createElement("div");
    box.style.cssText =
        "max-width:min(560px,90vw);max-height:80vh;overflow:auto;padding:1.2rem 1.4rem;" +
        "border-radius:10px;background:var(--comfy-menu-bg,#20202a);color:#eee;" +
        "box-shadow:0 8px 40px rgba(0,0,0,.6);font-size:.85rem;line-height:1.5;";
    const h = document.createElement("h3");
    h.textContent = title;
    h.style.cssText = "margin:0 0 .6rem;font-size:1rem;";
    box.appendChild(h);

    const section = (label, items, color) => {
        if (!items || !items.length) return;
        const t = document.createElement("div");
        t.textContent = label;
        t.style.cssText = `margin:.6rem 0 .25rem;font-weight:600;color:${color};`;
        const ul = document.createElement("ul");
        ul.style.cssText = "margin:0;padding-left:1.1rem;";
        for (const it of items) {
            const li = document.createElement("li");
            li.textContent = it;
            li.style.marginBottom = ".2rem";
            ul.appendChild(li);
        }
        box.append(t, ul);
    };
    section("Errors — node not generated", report.errors, "#f77");
    section("Hints", report.warnings, "#f5b942");
    if (report.baked_inputs?.length) {
        section("Baked to defaults (not editable on the instant node)",
            report.baked_inputs.map((b) => `${b.name} = ${JSON.stringify(b.value)}`),
            "#8bd");
    }

    const close = document.createElement("button");
    close.textContent = "Close";
    close.style.cssText =
        "margin-top:1rem;padding:.4rem .9rem;border-radius:6px;border:none;cursor:pointer;" +
        "background:var(--p-primary-color,#4f9cf9);color:#fff;font-size:.85rem;";
    close.onclick = () => overlay.remove();
    box.appendChild(close);
    overlay.appendChild(box);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
    document.body.appendChild(overlay);
}

// Create the pre-filled generic runner node (not yet positioned/wired).
function createGenericNode(report) {
    const node = LiteGraph.createNode(GENERIC_NODE_TYPE);
    if (!node) {
        toast("error", "Comfy Cloud",
            "Generic runner node not found — restart ComfyUI so the pack loads.");
        return null;
    }
    app.graph.add(node);
    const w = node.widgets?.find((x) => x.name === "workflow_json");
    if (w) w.value = report.generic_json;
    node.title = `☁ ${report.name || "Cloud"} (test)`;
    return node;
}

function getLink(id) {
    const links = app.graph.links;
    if (!links || id == null) return null;
    return typeof links.get === "function" ? links.get(id) : links[id];
}

// Insert next to the source subgraph, keep the subgraph untouched.
function insertGenericNode(sourceNode, report) {
    const node = createGenericNode(report);
    if (!node) return;
    if (sourceNode?.pos) node.pos = [sourceNode.pos[0], sourceNode.pos[1] + 160];
    app.graph.setDirtyCanvas(true, true);
    const imgs = (report.image_inputs || []).map((i) => `${i.token} ← ${i.name}`).join(", ");
    toast("success", "Comfy Cloud",
        `Instant node created for “${report.name}”.` +
        (imgs ? ` Connect image inputs: ${imgs}.` : ""));
}

// Swap the subgraph for the generic node: rewire incoming image links to
// image_1…N (by boundary-input name), move IMAGE-output links onto the
// generic node's single IMAGE output, then remove the subgraph.
function replaceWithGenericNode(sourceNode, report) {
    const node = createGenericNode(report);
    if (!node) return;
    const skipped = [];
    try {
        (report.image_inputs || []).forEach((map, i) => {
            const srcIdx = (sourceNode.inputs || []).findIndex(
                (inp) => inp.label === map.name || inp.name === map.name);
            const linkId = srcIdx >= 0 ? sourceNode.inputs[srcIdx].link : null;
            const link = getLink(linkId);
            const origin = link && app.graph.getNodeById(link.origin_id);
            const dstIdx = (node.inputs || []).findIndex(
                (inp) => inp.name === `image_${i + 1}`);
            if (origin && dstIdx >= 0) origin.connect(link.origin_slot, node, dstIdx);
        });
        const imgOut = (sourceNode.outputs || []).findIndex((o) => o.type === "IMAGE");
        (sourceNode.outputs || []).forEach((out, oi) => {
            for (const lid of [...(out.links || [])]) {
                const link = getLink(lid);
                const target = link && app.graph.getNodeById(link.target_id);
                if (!target) continue;
                if (oi === imgOut) node.connect(0, target, link.target_slot);
                else skipped.push(out.label || out.name || out.type);
            }
        });
        node.pos = [...sourceNode.pos];
        app.graph.remove(sourceNode);
    } catch (e) {
        console.warn("[ComfyCloudHybrid] replace failed:", e);
        toast("warn", "Comfy Cloud",
            `Rewiring failed (${e.message || e}) — the cloud node was inserted, ` +
            "the subgraph was kept.");
        if (sourceNode?.pos) node.pos = [sourceNode.pos[0], sourceNode.pos[1] + 160];
        app.graph.setDirtyCanvas(true, true);
        return;
    }
    app.graph.setDirtyCanvas(true, true);
    toast("success", "Comfy Cloud",
        `Subgraph replaced by “${node.title}”.` +
        (skipped.length ? ` Not rewired (no slot on the instant node): ` +
            [...new Set(skipped)].join(", ") + "." : ""));
}

// Success banner: Insert / Replace / Cancel + mapping info and hints.
function showConvertActions(sourceNode, report) {
    const overlay = document.createElement("div");
    overlay.style.cssText =
        "position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:3000;" +
        "display:flex;align-items:center;justify-content:center;";
    const box = document.createElement("div");
    box.style.cssText =
        "max-width:min(560px,90vw);max-height:80vh;overflow:auto;padding:1.2rem 1.4rem;" +
        "border-radius:10px;background:var(--comfy-menu-bg,#20202a);color:#eee;" +
        "box-shadow:0 8px 40px rgba(0,0,0,.6);font-size:.85rem;line-height:1.5;";
    const h = document.createElement("h3");
    h.textContent = `Cloud node ready — ${report.name || "subgraph"}`;
    h.style.cssText = "margin:0 0 .6rem;font-size:1rem;";
    box.appendChild(h);

    const addList = (label, items, color) => {
        if (!items || !items.length) return;
        const t = document.createElement("div");
        t.textContent = label;
        t.style.cssText = `margin:.6rem 0 .25rem;font-weight:600;color:${color};`;
        const ul = document.createElement("ul");
        ul.style.cssText = "margin:0;padding-left:1.1rem;";
        for (const it of items) {
            const li = document.createElement("li");
            li.textContent = it;
            ul.appendChild(li);
        }
        box.append(t, ul);
    };
    addList("Image inputs", (report.image_inputs || []).map(
        (i) => `${i.token} ← ${i.name}`), "#8bd");
    addList("Baked to defaults", (report.baked_inputs || []).map(
        (b) => `${b.name} = ${JSON.stringify(b.value)}`), "#8bd");
    addList("Hints", report.warnings, "#f5b942");

    const row = document.createElement("div");
    row.style.cssText = "display:flex;gap:.6rem;margin-top:1rem;";
    const mkBtn = (label, primary, fn) => {
        const b = document.createElement("button");
        b.textContent = label;
        b.style.cssText =
            "padding:.4rem .9rem;border-radius:6px;border:none;cursor:pointer;" +
            "font-size:.85rem;" + (primary
                ? "background:var(--p-primary-color,#4f9cf9);color:#fff;"
                : "background:#3a3a44;color:#ddd;");
        b.onclick = () => { overlay.remove(); if (fn) fn(); };
        return b;
    };
    row.append(
        mkBtn("Replace subgraph", true, () => replaceWithGenericNode(sourceNode, report)),
        mkBtn("Insert next to it", false, () => insertGenericNode(sourceNode, report)),
        mkBtn("Cancel", false, null));
    box.appendChild(row);
    overlay.appendChild(box);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
    document.body.appendChild(overlay);
}

async function convertSubgraph(node, mode) {
    let blueprint;
    try {
        blueprint = subgraphToBlueprint(node);
    } catch (e) {
        toast("error", "Comfy Cloud", `Cannot read this subgraph: ${e.message || e}`);
        return;
    }
    toast("info", "Comfy Cloud", mode === "save"
        ? "Validating and saving subgraph …" : "Validating subgraph …");
    let report;
    try {
        report = await postJson("/cloudhybrid/convert", { blueprint, mode });
    } catch (e) {
        toast("error", "Comfy Cloud", `Conversion request failed: ${e}`);
        return;
    }

    if (!report.ok) {
        toast("error", "Comfy Cloud",
            `Cannot convert “${report.name || "subgraph"}” — ${(report.errors || []).length} error(s).`);
        showReportDialog("Cannot convert this subgraph", report);
        return;
    }

    if (mode === "save") {
        if (report.saved) {
            toast("warn", "Comfy Cloud",
                `Saved as “${report.name}”. Restart ComfyUI to get the node` +
                (report.restart_required === false ? "." : " (new node classes need a restart)."));
        } else {
            toast("error", "Comfy Cloud", "Saving failed — see report.");
            showReportDialog("Save failed", report);
        }
        if ((report.warnings || []).length) showReportDialog("Saved — please note", report);
        return;
    }

    // mode === "test"
    if (report.instant_testable) {
        showConvertActions(node, report);
    } else {
        toast("warn", "Comfy Cloud",
            `“${report.name}” can't run on the instant node (${report.generic_reason}). ` +
            "Use “Save as Cloud Node” for a full node with all inputs.");
        showReportDialog("Not instant-testable — use Save as Cloud Node", report);
    }
}

// Add the two entries to any subgraph node's right-click menu.
function installSubgraphMenu() {
    if (typeof LGraphCanvas === "undefined" || !LGraphCanvas.prototype) return;
    const orig = LGraphCanvas.prototype.getNodeMenuOptions;
    LGraphCanvas.prototype.getNodeMenuOptions = function (node) {
        const options = orig ? orig.apply(this, arguments) : [];
        if (isSubgraphNode(node)) {
            options.push(null, {
                content: "☁ Convert to Cloud API Node (test)",
                callback: () => convertSubgraph(node, "test"),
            }, {
                content: "☁ Save as Cloud Node (permanent)",
                callback: () => convertSubgraph(node, "save"),
            });
        }
        return options;
    };
}

app.registerExtension({
    name: "ComfyCloudHybrid",
    settings: [
        {
            id: SETTING_KEY,
            name: "API key",
            category: ["Comfy Cloud Hybrid", "Connection", "API Key"],
            defaultValue: "",
            tooltip: "Sent to your local ComfyUI server and stored server-side — " +
                "it never persists in the frontend settings.",
            // custom renderer: password input + subtle "Get your key here ⓘ"
            // line underneath (native title tooltips are unreliable inside the
            // settings dialog, so the info icon gets its own hover element)
            type: () => {
                const wrap = document.createElement("div");
                wrap.style.cssText =
                    "position:relative;display:flex;flex-direction:column;" +
                    "align-items:flex-end;gap:.3rem;";

                const input = document.createElement("input");
                input.type = "password";
                input.placeholder = "Paste key from platform.comfy.org";
                input.autocomplete = "off";
                input.style.cssText =
                    "width:17rem;padding:.45rem .6rem;border-radius:6px;" +
                    "border:1px solid var(--p-form-field-border-color,#4a4a52);" +
                    "background:var(--p-form-field-background,#18181b);" +
                    "color:inherit;font-size:.85rem;outline:none;";
                input.addEventListener("change", async () => {
                    const value = input.value.trim();
                    if (!value) return;
                    try {
                        const result = await postJson("/cloudhybrid/api_key",
                            { api_key: value });
                        toast("success", "Comfy Cloud",
                            `API key saved (${result.masked || "ok"}).`);
                        input.value = ""; // key never stays in the frontend
                        input.placeholder = `Key saved (${result.masked || "ok"})`;
                    } catch (e) {
                        toast("error", "Comfy Cloud", `Saving the key failed: ${e}`);
                    }
                });

                const help = document.createElement("div");
                help.style.cssText =
                    "display:flex;align-items:center;gap:.35rem;" +
                    "font-size:.78rem;opacity:.65;";
                const label = document.createElement("span");
                label.append("Get your key ");
                const link = document.createElement("a");
                link.href = API_KEY_URL;
                link.target = "_blank";
                link.rel = "noopener";
                link.textContent = "here";
                link.style.cssText =
                    "color:var(--p-primary-color,#4f9cf9);" +
                    "text-decoration:underline;";
                label.appendChild(link);

                const info = document.createElement("i");
                info.className = "pi pi-info-circle";
                info.style.cssText = "cursor:help;";

                const tip = document.createElement("div");
                tip.textContent =
                    "Comfy Dev Platform → Profile → API Keys → New.\n" +
                    "The key is shown only once — paste it right away.\n" +
                    "Requires a paid Comfy Cloud plan (Standard/Creator/Pro); " +
                    "the free tier has no API access.";
                tip.style.cssText =
                    "position:absolute;top:100%;right:0;margin-top:.3rem;" +
                    "max-width:270px;padding:.5rem .65rem;border-radius:6px;" +
                    "background:var(--p-tooltip-background,#2b2b33);color:#eee;" +
                    "font-size:.75rem;line-height:1.4;white-space:pre-line;" +
                    "box-shadow:0 2px 10px rgba(0,0,0,.5);display:none;" +
                    "z-index:2000;text-align:left;";
                info.addEventListener("mouseenter", () => {
                    tip.style.display = "block";
                });
                info.addEventListener("mouseleave", () => {
                    tip.style.display = "none";
                });

                help.append(label, info);
                wrap.append(input, help, tip);
                return wrap;
            },
        },
        {
            id: "CloudHybrid.JobTimeout",
            name: "Job timeout — render phase (seconds)",
            tooltip: "Maximum RENDER time. Starts counting only once a cloud " +
                "worker is actually executing the job — queue and model-loading " +
                "time does not count against it.",
            category: ["Comfy Cloud Hybrid", "Execution", "Job timeout"],
            type: "number",
            defaultValue: 1800,
            attrs: { min: 60, max: 3600, step: 60 },
            onChange: async (value, oldValue) => {
                if (oldValue === undefined) return; // page-load event
                try { await postJson("/cloudhybrid/config", { job_timeout_s: Number(value) }); }
                catch (e) { toast("error", "Comfy Cloud", `Saving failed: ${e}`); }
            },
        },
        {
            id: "CloudHybrid.QueueTimeout",
            name: "Queue timeout — waiting phase (seconds)",
            tooltip: "How long to wait for a free cloud worker (including model " +
                "loading on a cold worker) before cancelling. Cancelling in this " +
                "phase consumes no render credits.",
            category: ["Comfy Cloud Hybrid", "Execution", "Queue timeout"],
            type: "number",
            defaultValue: 900,
            attrs: { min: 30, max: 3600, step: 30 },
            onChange: async (value, oldValue) => {
                if (oldValue === undefined) return;
                try { await postJson("/cloudhybrid/config", { queue_timeout_s: Number(value) }); }
                catch (e) { toast("error", "Comfy Cloud", `Saving failed: ${e}`); }
            },
        },
        {
            id: "CloudHybrid.PollInterval",
            name: "Poll interval (seconds)",
            tooltip: "How often the job status is polled while a cloud job runs.",
            category: ["Comfy Cloud Hybrid", "Execution", "Poll interval"],
            type: "number",
            defaultValue: 2,
            attrs: { min: 1, max: 30, step: 1 },
            onChange: async (value, oldValue) => {
                if (oldValue === undefined) return;
                try { await postJson("/cloudhybrid/config", { poll_interval_s: Number(value) }); }
                catch (e) { toast("error", "Comfy Cloud", `Saving failed: ${e}`); }
            },
        },
        {
            id: "CloudHybrid.SkipLocalCapable",
            name: "Skip model-free blueprints",
            tooltip: "Blueprints without any AI model (GLSL filters, crops, video " +
                "utilities) run faster and free on your local machine — by default " +
                "no cloud nodes are registered for them. Takes effect after a " +
                "ComfyUI restart.",
            category: ["Comfy Cloud Hybrid", "Blueprints", "Skip model-free"],
            type: "boolean",
            defaultValue: true,
            onChange: async (value, oldValue) => {
                if (oldValue === undefined) return;
                try { await postJson("/cloudhybrid/config", { skip_local_capable: Boolean(value) }); }
                catch (e) { toast("error", "Comfy Cloud", `Saving failed: ${e}`); }
            },
        },
    ],
    commands: [
        {
            id: "cloudhybrid.getkey",
            label: "Comfy Cloud: Open API key page (platform.comfy.org)",
            function: openApiKeyPage,
        },
        {
            id: "cloudhybrid.test",
            label: "Comfy Cloud: Test connection",
            function: testConnection,
        },
        {
            id: "cloudhybrid.rescan",
            label: "Comfy Cloud: Rescan blueprints",
            function: rescanBlueprints,
        },
    ],
    async setup() {
        try { installSubgraphMenu(); }
        catch (e) { console.warn("[ComfyCloudHybrid] subgraph menu not installed:", e); }
        // surface server-side key state once at startup
        try {
            const resp = await api.fetchApi("/cloudhybrid/status");
            const status = await resp.json();
            if (status.key_source === "none") {
                toast("warn", "Comfy Cloud Hybrid",
                    "No API key configured. Create one at " + API_KEY_URL +
                    " and paste it under Settings → Comfy Cloud Hybrid " +
                    "(or run the command 'Comfy Cloud: Open API key page').");
            }
        } catch (e) { /* server without routes (import failed) — stay quiet */ }
    },
});
