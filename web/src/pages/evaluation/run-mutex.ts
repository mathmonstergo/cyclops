export type EvaluationRunLease = symbol

export type EvaluationRunMutex = {
  tryAcquire: () => EvaluationRunLease | null
  release: (lease: EvaluationRunLease) => void
}

// 创建评测运行互斥器；关键约束是只有当前 lease 能释放单条/批量共用的锁。
export function createEvaluationRunMutex(): EvaluationRunMutex {
  let activeLease: EvaluationRunLease | null = null
  return {
    tryAcquire: () => {
      if (activeLease !== null) return null
      activeLease = Symbol('evaluation-run')
      return activeLease
    },
    release: (lease) => {
      if (activeLease === lease) activeLease = null
    },
  }
}
