# Tooltip 延迟与抽屉初始焦点行业模式调研

调研日期：2026-07-15（Asia/Shanghai）

## 当前问题

打开多个抽屉时，文件 ID 复制等 tooltip 会在鼠标没有移动的情况下立即出现。用户还提出是否应“首次延迟，短时间查看多个后降低延迟”。

## 根因与官方行为

项目使用 Radix Dialog/Tooltip。Radix Tooltip 官方定义是：trigger 收到键盘 focus 或鼠标 hover 时显示。其实现有两条不同路径：

- pointer 进入时遵守 `delayDuration`；默认 700ms；
- focus 时直接 `onOpen()`，不经过 pointer delay。

Radix Provider 已内置 warm-up/skip-delay：第一个 tooltip 成功打开后，`skipDelayDuration` 时间内进入另一个 trigger 会立即打开；默认 300ms，窗口结束后恢复首次延迟。它不是累计“看过 3 个”后加速，而是一次成功展示就进入短时快速浏览模式。

因此只调大 `delayDuration` 不能修复当前异常：Dialog 打开后自动把焦点交给第一个可聚焦按钮，而第一个按钮恰好是 Copy ID，focus 会立即打开 tooltip。

## 抽屉初始焦点标准

W3C WAI-ARIA Modal Dialog Pattern 要求打开对话框时焦点进入对话框，但并不要求永远聚焦第一个按钮。标准明确建议：

- 对包含列表、表格、多段内容等语义结构的复杂对话框，把 `tabindex="-1"` 放在内容开头的静态元素并将初始焦点放在那里；
- 内容较长、聚焦首个交互元素会把顶部滚出视口时，初始焦点应放到标题或首段静态内容；
- 不可逆操作的最终确认对话框，优先聚焦最不具破坏性的动作。

管理后台抽屉通常包含状态、元数据、列表和表单，属于复杂内容；把初始焦点放在抽屉标题/内容容器更符合标准，也能避免复制或危险按钮因程序化 focus 触发 tooltip。

## 推荐方案（待用户确认）

1. 共享 `DrawerContent` 默认阻止 Radix focus-first，并显式聚焦标题或内容容器；容器 `tabIndex={-1}`。
2. 需要立即录入的简单表单抽屉允许显式传 `initialFocusRef`，但不做隐式 DOM 顺序推断。
3. 保留 tooltip 对键盘 focus 的即时反馈，不能为了消除自动弹出而禁用 focus tooltip。
4. 全局 pointer 首次延迟采用 500ms，`skipDelayDuration` 采用 400ms：第一次扫过不会产生噪音，一个 tooltip 展示后短时间浏览相邻图标可即时显示。
5. 不实现“累计查看 3 个后加速”的隐藏计数状态；Radix 原生 warm-up 已覆盖真实需求，行为更可预测。
6. 打开文档、FAQ、KG、评测等抽屉后验证：无鼠标移动时 tooltip 不出现；Tab 仍可到达复制按钮并即时获得提示；关闭后焦点回到触发元素。

## 来源

- Radix Tooltip 官方文档：https://www.radix-ui.com/primitives/docs/components/tooltip
- Radix Tooltip 官方实现（默认 700ms、skip 300ms、focus 即时打开）：https://github.com/radix-ui/primitives/blob/main/packages/react/tooltip/src/tooltip.tsx
- W3C WAI-ARIA Modal Dialog Pattern：https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/

