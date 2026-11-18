const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('popup displays storage and runtime failures instead of rejecting its event handler', async () => {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, { listeners: {}, addEventListener(event, callback) { this.listeners[event] = callback; } });
    return nodes.get(id);
  };
  const context = vm.createContext({ Date, URL,
    document: { getElementById: node },
    chrome: { storage: { local: {
      get: async () => { throw Error('storage failed'); },
      set: async () => { throw Error('write failed'); }
    }, onChanged: { addListener() {} } } }
  });
  vm.runInContext(fs.readFileSync('chrome-extension/popup.js', 'utf8'), context);
  await new Promise(setImmediate);
  assert.equal(node('err').textContent, 'storage failed');
  await node('enabled').listeners.change();
  assert.equal(node('err').textContent, 'write failed');
});
