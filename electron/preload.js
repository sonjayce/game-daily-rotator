// 渲染层安全桥 (contextIsolation)
'use strict'
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('api', {
  init: () => ipcRenderer.invoke('init'),
  start: () => ipcRenderer.invoke('start'),
  stop: () => ipcRenderer.invoke('stop'),
  tool: (name) => ipcRenderer.invoke('tool', name),
  setEnabled: (gid, val) => ipcRenderer.invoke('set-enabled', gid, val),
  setOrder: (order) => ipcRenderer.invoke('set-order', order),
  openLogs: () => ipcRenderer.invoke('open-logs'),
  emuAction: (action) => ipcRenderer.invoke('emu-action', action),
  saveSettings: (payload) => ipcRenderer.invoke('save-settings', payload),
  getHistory: () => ipcRenderer.invoke('get-history'),
  addGame: (gid, spec) => ipcRenderer.invoke('add-game', gid, spec),
  updateGame: (gid, spec) => ipcRenderer.invoke('update-game', gid, spec),
  removeGame: (gid) => ipcRenderer.invoke('remove-game', gid),
  testGame: (gid) => ipcRenderer.invoke('test-game', gid),
  chooseFile: (filters) => ipcRenderer.invoke('choose-file', filters),
  chooseDir: () => ipcRenderer.invoke('choose-dir'),
  on: (channel, cb) => ipcRenderer.on(channel, (_e, payload) => cb(payload)),
})
