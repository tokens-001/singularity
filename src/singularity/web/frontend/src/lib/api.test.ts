// ponytail: 确保 API 对象结构完整，不发无效请求
import { describe, it, expect } from 'vitest'
import { api } from './api'

describe('api', () => {
  it('has all expected methods', () => {
    expect(typeof api.status).toBe('function')
    expect(typeof api.tasks).toBe('function')
    expect(typeof api.projects).toBe('function')
    expect(typeof api.agents).toBe('function')
    expect(typeof api.models).toBe('function')
    expect(typeof api.skills).toBe('function')
    expect(typeof api.apiStore).toBe('function')
    expect(typeof api.observerChat).toBe('function')
  })

  it('task operations exist', () => {
    expect(typeof api.createTask).toBe('function')
    expect(typeof api.cancelTask).toBe('function')
    expect(typeof api.retryTask).toBe('function')
    expect(typeof api.holdTask).toBe('function')
    expect(typeof api.releaseTask).toBe('function')
  })

  it('model operations exist', () => {
    expect(typeof api.addModel).toBe('function')
    expect(typeof api.deleteModel).toBe('function')
    expect(typeof api.importModels).toBe('function')
    expect(typeof api.scanApiStore).toBe('function')
  })

  it('skill operations exist', () => {
    expect(typeof api.addSkill).toBe('function')
    expect(typeof api.deleteSkill).toBe('function')
    expect(typeof api.agentSkills).toBe('function')
    expect(typeof api.updateAgentSkills).toBe('function')
  })
})
