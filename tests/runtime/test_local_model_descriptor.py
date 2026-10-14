"""The local model page accepts only explicit Gateway model reports."""
from pathlib import Path
import subprocess


def test_local_model_descriptor_contract():
    root = Path(__file__).resolve().parents[2]
    script = """
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('bianbu_agent/console/web/static/js/console.js', 'utf8');
const constants = source.slice(source.indexOf('const LOCAL_MODEL_TYPES'), source.indexOf('const localModelState'));
const validator = source.slice(source.indexOf('function validateLocalModels'), source.indexOf('function applyLocalModels'));
const context = {t: key => key};
vm.createContext(context);
vm.runInContext(constants + validator, context);
const row = (model_type, name, path = null, artifact = null, backend = 'ai_gateway') => ({model_type, name, artifact, backend, path, port: null,
    download: {status: 'completed', progress: 100}, server: {status: 'running'}});
const gateway = {models: [row('llm', 'bibit-llm', '/home/bianbu/.cache/models/llm/glm-edge-1.5b-chat-q4_0.gguf', 'glm-edge-1.5b-chat-q4_0.gguf'),
    row('embed', 'bibit-embed')]};
assert.equal(context.validateLocalModels(gateway), gateway);
// A Gateway that reports no model yet is a valid read-only state.
assert.deepEqual(context.validateLocalModels({models: []}).models, []);
for (const patch of [{backend: null}, {name: ''}, {name: null}, {port: -1}, {artifact: 7},
    {port: '18920'}, {port: 65536}, {model_type: 'other'}, {model_type: '__proto__'},
    {download: null}, {server: null}, {download: {status: 'completed', progress: 'nope'}}]) {
    const invalid = structuredClone(gateway);
    Object.assign(invalid.models[0], patch);
    assert.throws(() => context.validateLocalModels(invalid));
}
assert.throws(() => context.validateLocalModels({models: [gateway.models[0], gateway.models[0]]}));
assert.throws(() => context.validateLocalModels({models: [null]}));
// Extra envelope keys such as the reported source stay acceptable.
assert.equal(context.validateLocalModels({models: gateway.models, source: 'ai-gateway'}).source, 'ai-gateway');
"""
    result = subprocess.run(["node", "-e", script], cwd=root, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
