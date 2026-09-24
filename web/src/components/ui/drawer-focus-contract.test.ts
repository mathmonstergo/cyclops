import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import react from '@vitejs/plugin-react'
import { Window } from 'happy-dom'
import { act, createElement, type ComponentType } from 'react'
import { createServer } from 'vite'

type RuntimeComponent = ComponentType<Record<string, unknown>>

const appShellSource = readFileSync(new URL('../layout/app-shell.tsx', import.meta.url), 'utf8')

// 为 Radix、ReactDOM 和 Framer Motion 安装同一套浏览器全局对象，确保事件与焦点属于同一文档。
function installDomGlobals(browserWindow: Window): void {
  const globals: Record<string, unknown> = {
    window: browserWindow,
    self: browserWindow,
    document: browserWindow.document,
    navigator: browserWindow.navigator,
    Node: browserWindow.Node,
    NodeFilter: browserWindow.NodeFilter,
    Element: browserWindow.Element,
    HTMLElement: browserWindow.HTMLElement,
    HTMLButtonElement: browserWindow.HTMLButtonElement,
    HTMLInputElement: browserWindow.HTMLInputElement,
    SVGElement: browserWindow.SVGElement,
    Document: browserWindow.Document,
    DocumentFragment: browserWindow.DocumentFragment,
    Event: browserWindow.Event,
    CustomEvent: browserWindow.CustomEvent,
    FocusEvent: browserWindow.FocusEvent,
    MouseEvent: browserWindow.MouseEvent,
    PointerEvent: browserWindow.PointerEvent,
    KeyboardEvent: browserWindow.KeyboardEvent,
    MutationObserver: browserWindow.MutationObserver,
    ResizeObserver: browserWindow.ResizeObserver,
    DOMRect: browserWindow.DOMRect,
    CSS: browserWindow.CSS,
    getComputedStyle: browserWindow.getComputedStyle.bind(browserWindow),
    requestAnimationFrame: browserWindow.requestAnimationFrame.bind(browserWindow),
    cancelAnimationFrame: browserWindow.cancelAnimationFrame.bind(browserWindow),
    IS_REACT_ACT_ENVIRONMENT: true,
  }

  for (const [name, value] of Object.entries(globals)) {
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value })
  }
}

// Vite 返回未知的 SSR 模块命名空间；这里只接受 React 可渲染的函数或对象组件。
function requireRuntimeComponent(module: unknown, exportName: string): RuntimeComponent {
  assert.ok(typeof module === 'object' && module !== null)
  const value = Reflect.get(module, exportName)
  assert.ok((typeof value === 'object' && value !== null) || typeof value === 'function')
  return value as RuntimeComponent
}

// 等待 React effect、Radix FocusScope 和 Portal 在当前浏览器任务中完成。
async function flushBrowserEffects(): Promise<void> {
  await act(async (): Promise<void> => {
    await new Promise<void>((resolve) => window.setTimeout(resolve, 0))
  })
}

test('AppShell configures the single tooltip warm-up contract', () => {
  assert.equal(appShellSource.match(/<TooltipProvider\b/g)?.length, 1)
  assert.match(
    appShellSource,
    /<TooltipProvider\s+delayDuration=\{500\}\s+skipDelayDuration=\{400\}>/,
  )
})

test('Drawer keeps Tooltip closed until keyboard focus and restores its trigger focus', async () => {
  const browserWindow = new Window({ url: 'http://localhost/' })
  installDomGlobals(browserWindow)

  const webRoot = fileURLToPath(new URL('../../../', import.meta.url))
  const vite = await createServer({
    appType: 'custom',
    configFile: false,
    root: webRoot,
    plugins: [react()],
    resolve: { alias: { '@': fileURLToPath(new URL('../../../src', import.meta.url)) } },
    server: { middlewareMode: true },
  })

  const drawerModule: unknown = await vite.ssrLoadModule('/src/components/ui/drawer.tsx')
  const tooltipModule: unknown = await vite.ssrLoadModule('/src/components/ui/tooltip.tsx')
  const { createRoot } = await import('react-dom/client')

  const Drawer = requireRuntimeComponent(drawerModule, 'Drawer')
  const DrawerTrigger = requireRuntimeComponent(drawerModule, 'DrawerTrigger')
  const DrawerClose = requireRuntimeComponent(drawerModule, 'DrawerClose')
  const DrawerContent = requireRuntimeComponent(drawerModule, 'DrawerContent')
  const DrawerTitle = requireRuntimeComponent(drawerModule, 'DrawerTitle')
  const TooltipProvider = requireRuntimeComponent(tooltipModule, 'TooltipProvider')
  const Tooltip = requireRuntimeComponent(tooltipModule, 'Tooltip')
  const TooltipTrigger = requireRuntimeComponent(tooltipModule, 'TooltipTrigger')
  const TooltipContent = requireRuntimeComponent(tooltipModule, 'TooltipContent')

  const container = document.createElement('div')
  document.body.append(container)
  const root = createRoot(container)

  try {
    await act(async (): Promise<void> => {
      root.render(
        createElement(
          TooltipProvider,
          { delayDuration: 500, skipDelayDuration: 400 },
          createElement(
            Drawer,
            null,
            createElement(
              DrawerTrigger,
              { asChild: true },
              createElement(
                'button',
                { 'data-testid': 'drawer-trigger', type: 'button' },
                '打开文档抽屉',
              ),
            ),
            createElement(
              DrawerContent,
              { 'data-testid': 'drawer-root' },
              createElement(DrawerTitle, null, '文档详情'),
              createElement(
                Tooltip,
                null,
                createElement(
                  TooltipTrigger,
                  { asChild: true },
                  createElement(
                    'button',
                    { 'data-testid': 'copy-id', type: 'button' },
                    '复制ID',
                  ),
                ),
                createElement(TooltipContent, null, '复制文件 ID'),
              ),
              createElement(
                DrawerClose,
                { asChild: true },
                createElement(
                  'button',
                  { 'data-testid': 'drawer-close', type: 'button' },
                  '关闭抽屉',
                ),
              ),
            ),
          ),
        ),
      )
    })

    const trigger = document.querySelector('[data-testid="drawer-trigger"]')
    assert.ok(trigger instanceof HTMLButtonElement)
    trigger.focus()
    assert.equal(document.activeElement, trigger)

    await act(async (): Promise<void> => trigger.click())
    await flushBrowserEffects()

    const drawerRoot = document.querySelector('[data-testid="drawer-root"]')
    const copyId = document.querySelector('[data-testid="copy-id"]')
    assert.ok(drawerRoot instanceof HTMLElement)
    assert.ok(copyId instanceof HTMLButtonElement)
    assert.equal(document.activeElement, drawerRoot)
    assert.notEqual(document.activeElement, copyId)
    assert.equal(document.querySelector('[role="tooltip"]'), null)

    await act(async (): Promise<void> => {
      drawerRoot.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Tab' }))
      copyId.focus()
    })
    assert.ok(document.querySelector('[role="tooltip"]'))

    const close = document.querySelector('[data-testid="drawer-close"]')
    assert.ok(close instanceof HTMLButtonElement)
    await act(async (): Promise<void> => close.click())
    await flushBrowserEffects()
    assert.equal(document.querySelector('[data-testid="drawer-root"]'), null)
    assert.equal(document.activeElement, trigger)
  } finally {
    await act(async (): Promise<void> => root.unmount())
    container.remove()
    await vite.close()
    await browserWindow.close()
  }
})
