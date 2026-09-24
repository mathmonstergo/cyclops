import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AppRoutes } from './app-routes'
import './index.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 5_000,
    },
  },
})

// 启动管理后台前显式校验挂载点，不用 non-null assertion 隐藏 HTML 缺失。
const rootElement = document.getElementById('root')
if (!rootElement) {
  throw new Error('Application root element is required')
}

createRoot(rootElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AppRoutes />
    </QueryClientProvider>
  </StrictMode>,
)
