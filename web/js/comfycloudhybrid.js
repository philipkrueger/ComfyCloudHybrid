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

app.registerExtension({
    name: "ComfyCloudHybrid",
    settings: [
        {
            id: SETTING_KEY,
            name: "API key (stored server-side)",
            category: ["Comfy Cloud Hybrid", "Connection", "API Key"],
            type: "text",
            defaultValue: "",
            attrs: { type: "password", placeholder: "Paste key from platform.comfy.org" },
            tooltip: "Create a key on the Comfy Dev Platform: " + API_KEY_URL +
                " (Profile → API Keys → New). The key is shown only once — paste it " +
                "here right away. Requires a paid Comfy Cloud plan (Standard/Creator/" +
                "Pro); the free tier has no API access. The key is sent to your " +
                "local ComfyUI server immediately and stored in a server-side file — " +
                "it never persists in the frontend settings.",
            onChange: async (value) => {
                if (!value) return; // initial load / after clearing
                try {
                    const result = await postJson("/cloudhybrid/api_key", { api_key: value });
                    toast("success", "Comfy Cloud",
                        `API key saved (${result.masked || "ok"}).`);
                } catch (e) {
                    toast("error", "Comfy Cloud", `Saving the key failed: ${e}`);
                    return;
                }
                // never persist the key in frontend settings storage
                try {
                    await app.extensionManager.setting.set(SETTING_KEY, "");
                } catch (e) { /* older frontends: value stays until reload */ }
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
