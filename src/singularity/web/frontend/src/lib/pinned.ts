// 项目置顶（localStorage）。侧边栏和项目页都要读，所以单独放一个模块，避免互相 import。
const KEY = 'qidian-pinned'

export function getPinned(): string[] {
  try { return JSON.parse(localStorage.getItem(KEY) || '[]') } catch { return [] }
}

export function togglePin(pid: string) {
  const pins = getPinned()
  localStorage.setItem(KEY, JSON.stringify(pins.includes(pid) ? pins.filter(p => p !== pid) : [pid, ...pins]))
}
