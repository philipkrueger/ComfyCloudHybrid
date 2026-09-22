import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../../web/js/comfycloudhybrid.js", import.meta.url), "utf8")
    .replace(/^import .*;\n/gm, "");

function setup() {
    const root = { subgraphs: new Map() };
    const graph = {
        rootGraph: root, nodes: new Map(), links: new Map(), nextId: 1, nextLink: 1,
        add(node) { node.id = this.nextId++; node.graph = this; this.nodes.set(node.id, node); },
        getNodeById(id) { return this.nodes.get(id); },
        remove(node) { this.nodes.delete(node.id); node.graph = null; },
        setDirtyCanvas() {},
    };
    function makeNode(type) {
        return {
            type, pos: [10, 20], properties: {},
            inputs: [{ name: type === "CloudHybrid_RunWorkflow" ? "image_1" : "image", link: null }],
            outputs: [{ name: "IMAGE", type: "IMAGE", links: [] }],
            widgets: [{ name: "workflow_json", value: "" }],
            configure(data) { Object.assign(this, structuredClone(data)); },
            connect(output, target, input) {
                assert.equal(this.graph, target.graph, "must connect within the owning graph");
                const id = graph.nextLink++;
                graph.links.set(id, { origin_id: this.id, origin_slot: output,
                    target_id: target.id, target_slot: input });
                this.outputs[output].links.push(id);
                target.inputs[input].link = id;
            },
        };
    }
    const errors = [];
    const context = vm.createContext({
        app: { graph: root, registerExtension() {},
            extensionManager: { toast: { add(message) {
                if (message.severity === "error") errors.push(message.detail);
            } } } },
        api: {}, console: { log() {}, warn() {} },
        LiteGraph: { createNode: makeNode },
    });
    vm.runInContext(source + "\nthis.actions = {insertGenericNode, replaceWithGenericNode, restoreSubgraph};", context);
    const origin = makeNode("origin");
    const subgraph = makeNode("subgraph-id");
    const target = makeNode("target");
    for (const node of [origin, subgraph, target]) graph.add(node);
    origin.connect(0, subgraph, 0);
    subgraph.connect(0, target, 0);
    root.subgraphs.set(subgraph.type, {});
    const blueprint = {
        nodes: [{ id: subgraph.id, type: subgraph.type,
            inputs: structuredClone(subgraph.inputs), outputs: structuredClone(subgraph.outputs) }],
        definitions: { subgraphs: [{ id: subgraph.type }] },
    };
    const report = { name: "Nested", generic_json: "{}", baked_inputs: [],
        image_inputs: [{ name: "image", token: "%CCH_IMAGE_1%" }] };
    return { ...context.actions, root, graph, origin, subgraph, target, blueprint, report, errors };
}

test("insert uses the source's graph even when the root graph is displayed", () => {
    const s = setup();
    s.insertGenericNode(s.subgraph, s.report, s.blueprint);
    const cloud = [...s.graph.nodes.values()].find(n => n.type === "CloudHybrid_RunWorkflow");
    assert.ok(cloud);
    assert.equal(cloud.graph, s.graph);
    assert.ok(s.graph.nodes.has(s.subgraph.id));
    assert.deepEqual(s.errors, []);
});

test("replace and restore preserve nested IMAGE connections and assigned ids", () => {
    const s = setup();
    s.replaceWithGenericNode(s.subgraph, s.report, s.blueprint);
    const cloud = [...s.graph.nodes.values()].find(n => n.type === "CloudHybrid_RunWorkflow");
    assert.ok(cloud);
    assert.ok(!s.graph.nodes.has(s.subgraph.id));
    assert.equal(s.graph.links.get(cloud.inputs[0].link).origin_id, s.origin.id);
    assert.equal(s.graph.links.get(s.target.inputs[0].link).origin_id, cloud.id);
    s.restoreSubgraph(cloud);
    const restored = [...s.graph.nodes.values()].find(n => n.type === "subgraph-id");
    assert.ok(restored);
    assert.notEqual(restored.id, s.subgraph.id);
    assert.ok(!s.graph.nodes.has(cloud.id));
    assert.equal(s.graph.links.get(restored.inputs[0].link).origin_id, s.origin.id);
    assert.equal(s.graph.links.get(s.target.inputs[0].link).origin_id, restored.id);
    assert.deepEqual(s.errors, []);
});

test("a removed source cannot insert into an unrelated active graph", () => {
    const s = setup();
    s.graph.remove(s.subgraph);
    s.insertGenericNode(s.subgraph, s.report, s.blueprint);
    assert.equal(s.graph.nodes.size, 2);
});
