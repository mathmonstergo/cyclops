# Component Guidelines

> Semantic HTML, empty states, and scrollbar patterns.

---

## Semantic HTML

Use proper HTML elements for accessibility and native browser behavior:

```tsx
// Good
<button onClick={handleClick}>Click me</button>

// Bad
<div role="button" onClick={handleClick}>Click me</div>
```

### Exception: Nested Interactive Elements

HTML does not allow `<button>` inside `<button>`. When a clickable card contains nested buttons (e.g., delete button), use `<div role="button">` for the outer container:

```tsx
// Good - Card with nested delete button
<div
  role="button"
  tabIndex={0}
  onClick={() => onClick(item)}
  onKeyDown={(e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onClick(item);
    }
  }}
  className="cursor-pointer focus-ring ..."
>
  <span>{item.title}</span>
  <button onClick={(e) => { e.stopPropagation(); onDelete(item.id); }}>
    Delete
  </button>
</div>

// Bad - Nested buttons cause hydration errors
<button onClick={() => onClick(item)}>
  <span>{item.title}</span>
  <button onClick={onDelete}>Delete</button>  {/* ERROR: button inside button */}
</button>
```

**Required attributes for `<div role="button">`:**

- `role="button"` - Accessibility role
- `tabIndex={0}` - Make it focusable
- `onKeyDown` - Handle Enter/Space keys
- `cursor-pointer` - Visual affordance

---

## Empty State Visual Centering

When displaying empty states in flex-column layouts with headers, use negative margin to achieve visual centering:

```tsx
// Good - Visual centering with negative margin offset
{
  !isLoading && items.length === 0 && (
    <div className="flex-1 flex items-center justify-center">
      <div className="-mt-24">
        <EmptyState />
      </div>
    </div>
  );
}

// Bad - Mathematical centering looks "off" visually
{
  !isLoading && items.length === 0 && (
    <div className="flex-1 flex items-center justify-center">
      <EmptyState />
    </div>
  );
}
```

**Why this pattern?**

- `flex-1 items-center` centers content in the **remaining space** below the header
- This creates mathematically correct but visually awkward positioning
- Negative margin (`-mt-24` to `-mt-32`) offsets the content upward for better visual balance

**Offset Guidelines:**

| Page Type                   | Offset   | Reason                               |
| --------------------------- | -------- | ------------------------------------ |
| Standard page header        | `-mt-24` | Compensates for ~80px header         |
| Header + action button/form | `-mt-32` | Additional offset for extra elements |

---

## Preventing Scrollbar Layout Shift

When a scrollable container's content grows to require a scrollbar, the scrollbar appearance can cause layout shift (content "jumps" left as scrollbar takes up space).

**Solution**: Use `scrollbar-gutter: stable` to reserve space for the scrollbar.

```tsx
// Good - Scrollbar space is always reserved
<main
  className="flex-1 overflow-y-auto p-6"
  style={{ scrollbarGutter: 'stable' }}
>
  {children}
</main>

// Bad - Content shifts when scrollbar appears/disappears
<main className="flex-1 overflow-y-auto p-6">
  {children}
</main>
```

**When to use:**

- Main content areas with `overflow-y-auto`
- Containers where content dynamically expands (e.g., tree views, lists)
- Any scrollable area where centered content (`mx-auto`) would shift

---

## Scrollbar Auto-Hide (Notion-inspired)

Scrollbars should be invisible by default and fade in/out smoothly on hover. This follows Notion's design language.

**Implementation (CSS)**:

```css
/* Global scrollbar styles */
::-webkit-scrollbar {
  width: 10px;
  height: 10px;
}

::-webkit-scrollbar-track {
  background: transparent;
}

::-webkit-scrollbar-thumb {
  background: hsl(var(--foreground) / 0);
  border-radius: 5px;
  transition: background 0.4s ease;
}

/* Show on container hover */
.scrollable:hover::-webkit-scrollbar-thumb {
  background: hsl(var(--foreground) / 0.12);
  transition: background 0.15s ease;
}

/* Darker on scrollbar hover */
.scrollable::-webkit-scrollbar-thumb:hover {
  background: hsl(var(--foreground) / 0.22);
}

/* Even darker when dragging */
.scrollable::-webkit-scrollbar-thumb:active {
  background: hsl(var(--foreground) / 0.32);
}
```

**Behavior:**

| State              | Opacity | Transition |
| ------------------ | ------- | ---------- |
| Default (hidden)   | 0%      | -          |
| Hover on content   | 12%     | 0.15s in   |
| Hover on scrollbar | 22%     | -          |
| Dragging           | 32%     | -          |
| Mouse leaves       | -> 0%   | 0.4s out   |

**Scroll Detection Hook** (for showing scrollbar during active scroll):

```tsx
import { useRef, useEffect, useState } from 'react';

function useScrolling<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [isScrolling, setIsScrolling] = useState(false);
  const timeoutRef = useRef<NodeJS.Timeout>();

  useEffect(() => {
    const element = ref.current;
    if (!element) return;

    const handleScroll = () => {
      setIsScrolling(true);
      clearTimeout(timeoutRef.current);
      timeoutRef.current = setTimeout(() => setIsScrolling(false), 1000);
    };

    element.addEventListener('scroll', handleScroll, { passive: true });
    return () => {
      element.removeEventListener('scroll', handleScroll);
      clearTimeout(timeoutRef.current);
    };
  }, []);

  return { ref, isScrolling };
}

// Usage
function MyScrollableList() {
  const { ref, isScrolling } = useScrolling<HTMLDivElement>();

  return (
    <div ref={ref} className={`overflow-y-auto scrollable ${isScrolling ? 'is-scrolling' : ''}`}>
      {/* content */}
    </div>
  );
}
```

---

## Toast Notifications

Implement a toast notification system for user feedback:

### Basic Usage

```tsx
import { useToast } from '../hooks/useToast';

function MyComponent() {
  const { toast } = useToast();

  const handleAction = async () => {
    try {
      await someAsyncOperation();
      toast.success('Operation completed!');
    } catch (error) {
      toast.error('Something went wrong');
    }
  };

  // Info toast for neutral messages
  toast.info('Processing...');
}
```

### Toast Types

| Type                     | Usage                      | Visual       |
| ------------------------ | -------------------------- | ------------ |
| `toast.success(message)` | Confirm successful actions | Green accent |
| `toast.error(message)`   | Report failures            | Red accent   |
| `toast.info(message)`    | Neutral information        | Muted accent |

### Design Principles

- Semi-transparent background (glassmorphism)
- Subtle border and shadow
- Pill-shaped or rounded
- Auto-dismiss after 3 seconds (configurable)
- Stack multiple toasts vertically

### Custom Duration

```tsx
// Custom duration (5 seconds)
toast.success('Saved!', 5000);

// Shorter duration (1.5 seconds)
toast.info('Copied', 1500);
```

---

## Drag and Drop (Tree Structures)

When implementing drag-and-drop for tree structures, use a library like `@dnd-kit`.

### Library Choice

Use **@dnd-kit** instead of `react-beautiful-dnd` (deprecated) because:

- Active maintenance and modern React support
- Official tree example for nested structures
- Better TypeScript support
- Modular architecture

### Architecture

```
components/dnd/
├── index.ts              # Public exports
├── tree-dnd-utils.ts     # Tree flattening & validation utilities
├── SortableTree.tsx      # DndContext wrapper component
├── SortableTreeItem.tsx  # Individual draggable tree item
└── TreeItemOverlay.tsx   # Drag preview overlay
```

### Key Concepts

#### 1. Tree Flattening

dnd-kit's `SortableContext` works best with flat lists. Flatten the tree while preserving hierarchy via `ancestorIds`:

```tsx
interface FlattenedTreeItem {
  node: TreeNode;
  depth: number;
  ancestorIds: string[]; // For circular reference prevention
  parentId: string | null;
  index: number;
}
```

#### 2. Drop Position Detection

For folders, divide the element into three zones:

- Top 20%: Drop "before" (as sibling above)
- Middle 60%: Drop "inside" (as child)
- Bottom 20%: Drop "after" (as sibling below)

For items (non-containers), it's simply top/bottom 50%.

#### 3. Validation Rules

- Cannot drop on itself
- Cannot drop into own descendants (circular reference)
- Cannot drop inside non-container items
- Cannot drag non-moveable items (system items)

### Basic Usage

```tsx
import { SortableTree } from './components/dnd';

<SortableTree
  nodes={treeNodes}
  expandedIds={expandedIds}
  onToggle={handleToggle}
  onNodeClick={handleNodeClick}
  onMoveNode={async (nodeId, nodeType, newParentId, newParentPath) => {
    // Call API to update parent
    await api.updateNode({ nodeId, parentId: newParentId });
  }}
  rootPath="/root"
  rootEntityId={rootId}
/>;
```

---

## Focus Management

### Focus Ring

Use consistent focus styles:

```css
/* Consistent focus ring */
.focus-ring:focus-visible {
  outline: 2px solid hsl(var(--color-primary));
  outline-offset: 2px;
}

/* Remove default focus for mouse users */
.focus-ring:focus:not(:focus-visible) {
  outline: none;
}
```

### Focus Trapping

For modals and dialogs, trap focus within the container:

```tsx
import { useRef, useEffect } from 'react';

function useFocusTrap<T extends HTMLElement>() {
  const ref = useRef<T>(null);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;

    const focusableElements = element.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    );

    const firstElement = focusableElements[0] as HTMLElement;
    const lastElement = focusableElements[focusableElements.length - 1] as HTMLElement;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return;

      if (e.shiftKey) {
        if (document.activeElement === firstElement) {
          e.preventDefault();
          lastElement.focus();
        }
      } else {
        if (document.activeElement === lastElement) {
          e.preventDefault();
          firstElement.focus();
        }
      }
    };

    element.addEventListener('keydown', handleKeyDown);
    firstElement?.focus();

    return () => element.removeEventListener('keydown', handleKeyDown);
  }, []);

  return ref;
}
```

---

## Customer Service Agent Route Layout Alignment

Internal tool pages in this project should keep route-level layout geometry aligned so page transitions can animate without visible jumps.

### Convention: Match The Assistant Shell

For primary workspace pages that need a left work list plus a main detail area, match the assistant page geometry:

- Route content root: `flex h-full min-h-0`
- Route-level left work panel: `w-[240px] shrink-0 border-r`
- Main panel: `flex min-w-0 flex-1 flex-col`
- Main panel header: `border-b bg-(--color-surface) px-5 py-3`
- Keep secondary editors, dictionaries, and inspectors in drawers unless they are the primary page surface.

```tsx
// Good: route geometry matches AssistantPage, so transitions do not jump.
<div className="flex h-full min-h-0">
  <aside className="flex h-full w-[240px] shrink-0 flex-col border-r border-(--color-border)">
    {/* list/search/filter work panel */}
  </aside>
  <main className="flex min-w-0 flex-1 flex-col">
    <header className="flex shrink-0 items-center gap-2 border-b border-(--color-border) bg-(--color-surface) px-5 py-3">
      {/* page context and actions */}
    </header>
    <div className="min-h-0 flex-1">{/* detail surface */}</div>
  </main>
</div>

// Bad: a full-width page toolbar above a custom-width left panel shifts the
// main detail header compared with AssistantPage.
<div className="flex h-full flex-col">
  <header>{/* page-wide toolbar */}</header>
  <div className="flex flex-1">
    <aside className="w-[320px]" />
    <main />
  </div>
</div>
```

**Why**: If feature pages choose different left-panel widths or place full-width toolbars above route content, switching between `/assistant` and the feature page moves the main header and detail surface. That makes later page transitions feel discontinuous even when each page looks acceptable in isolation.

### Assistant Message Navigation

Assistant message navigation should act as a quiet scroll aid rather than a second transcript:

- The right-side quick locator only lists user questions; do not include AI answers in the expanded locator.
- Expanded locator rows should be one-line, truncated question text with no visible role label, sequence number, timestamp, or other metadata.
- The collapsed rail should use at most 20 larger horizontal markers, sampled from the available user questions while preserving the first and last question.
- A floating "back to latest" action may appear when the user scrolls away from the bottom; it should disappear once the latest message is visible.
- Entering or switching to a conversation must position the message stream at the latest message by default. Treat `conversationId` changes as a scroll lifecycle event instead of relying only on message-array changes; within the same conversation, keep the user's manual reading position when assistant streaming updates arrive far from the bottom.

### Knowledge Graph Review UI

Knowledge graph review pages must use the existing internal-tool structure instead of a permanent three-column graph workspace:

- Route content root: `flex h-full flex-col`.
- Top toolbar: search, entity/relation mode, review status filter, type filter, and refresh.
- Main content: one dense list/table for either entities or relations.
- Detail surface: right-side drawer opened from a selected row, containing metadata, evidence, local relationships, and review actions.
- Extraction results must show review state explicitly: `needs_review`, `usable`, and `disabled`.
- Evidence must be visible in the drawer before confirmation; KG candidates are AI-generated and must not be confirmed from name/type alone.
- Review-table live values come from required backend fields: entity `source_count` is labeled `有效来源`, relation `evidence_count` is labeled `有效证据`, and subgraph edges require their own `evidence_count`. The complete `evidence` array is audit history only; its length may appear under `证据历史` but must never stand in for a live count. Rows with `is_valid=false` stay visible with a low-contrast `来源已失效` marker.
- All KG review reads refetch on mount because backend workers can change source/KG state while the route is inactive. Every successful source or KG mutation reuses the single three-family KG invalidator; component-local Drawer effects must not own cache correctness.
- Every entity/relation row and selected drawer item carries the backend `review_revision` positive integer. The confirm mutation sends exactly `{"expected_revision": item.review_revision}`; it never sends `{}`, omits the body, substitutes a timestamp, or defaults the revision.
- A 409 confirm response means the reviewed snapshot was replaced. Do not show a success toast or optimistically keep `usable`; show a refresh-required error and invalidate/refetch the list/detail so the user reviews the new snapshot.
- Frontend source and the built `cyclops/static/dist` bundle must both contain the revision contract. Rebuild after hook/schema changes; a green source test does not validate the ASGI-mounted stale bundle.
- 3D or force-graph visualization is a later view over confirmed KG data, not the first MVP review surface.

```tsx
// Good: KG review keeps the same page geometry as FAQ management.
<div className="flex h-full flex-col">
  <header>{/* search + review filters + refresh */}</header>
  <main>{/* entity or relation list */}</main>
  <KgDetailDrawer selected={selected} />
</div>

// Bad: three permanent panes compete with the existing app shell and make
// evidence review harder to scan.
<div className="grid grid-cols-[280px_1fr_360px]">{/* ... */}</div>
```

### Document Chunk IDs for KG Workflows

When a workflow needs internal document IDs or chunk IDs, the UI must expose them in the source surface instead of requiring users to inspect API responses:

- Document drawers should put the import file ID copy action beside the title, using the same compact `复制ID` action as chunk toolbars.
- Chunk toolbars may show only a compact `复制ID` action with a copy icon; avoid rendering the full chunk ID in crowded toolbars.
- Chunk toolbar section titles should stay visually capped at about 8 Chinese characters (`max-w-[8em]` with truncation) so action controls do not shift when a section name is long.
- Chunk toolbar controls should preserve a dense single-row height: prefer compact padding and 24px action buttons over default-height buttons in this row.
- Copy button hover text and success toast should include the full ID, and the copy action must copy the full ID.
- Do not use browser-native `title` tooltips in document drawers or chunk toolbars; use the shared Tooltip/Popover surfaces so hover and dropdown styling stays consistent.
- Chunk location metadata should prefer compact human locators such as `p14-15`; parser block types like `text` should not be shown unless they add clear user value.
- Chunk and KG evidence page locators must treat `page_start = 0` as a valid page, because some parser/provider outputs are zero-based or include a cover page. Use explicit nullish checks instead of truthy checks.
- Shared drawer overlays should animate dimming and blur progressively with the drawer entrance; avoid instant dark overlays followed by panel motion.
- Shared drawers should slide in from just outside the right edge with restrained easing, rather than appearing through a short fade/offset that feels like a popup.
- KG extraction from a document chunk should be available from the active chunk toolbar when the chunk is usable and not being edited.
- KG extraction from a FAQ should be available from that FAQ's management drawer when the FAQ is usable.
- Do not make users manually copy a chunk ID into the KG page for the common single-chunk extraction path.
- The KG review page has no manual extraction form. Source pages must send the one explicit locator shape: `source_type` plus `source_id`.

```tsx
// Good: the user can copy or act from the source chunk.
<ChunkToolbar>
  <CopyIdInline label="切片ID" value={chunk.id} />
  <Button onClick={() => extractKg({ source_type: 'document_chunk', source_id: chunk.id })}>
    KG 抽取
  </Button>
</ChunkToolbar>
```

```tsx
// Wrong: drops page 0.
if (chunk.page_start) {
  return `p${chunk.page_start}`
}

// Correct: only null/undefined means the locator is absent.
if (chunk.page_start !== null && chunk.page_start !== undefined) {
  return `p${chunk.page_start}`
}
```

---

## Loading States

### Skeleton Loading

```tsx
function Skeleton({ className }: { className?: string }) {
  return <div className={`animate-pulse bg-muted rounded ${className}`} />;
}

// Usage
function ListSkeleton() {
  return (
    <div className="space-y-2">
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-10 w-3/4" />
    </div>
  );
}
```

### Loading Button

```tsx
function Button({ isLoading, children, ...props }: ButtonProps) {
  return (
    <button disabled={isLoading} {...props}>
      {isLoading ? (
        <span className="inline-flex items-center gap-2">
          <Spinner className="h-4 w-4" />
          Loading...
        </span>
      ) : (
        children
      )}
    </button>
  );
}
```

---

## Quick Reference

| Pattern                    | When to Use                 |
| -------------------------- | --------------------------- |
| `role="button"` + tabIndex | Nested interactive elements |
| `-mt-24` offset            | Empty state centering       |
| `scrollbar-gutter: stable` | Prevent layout shift        |
| Auto-hide scrollbar        | Any scrollable container    |
| Toast notifications        | User feedback               |
| Focus trap                 | Modals/dialogs              |
| Skeleton loading           | Initial data fetch          |
| Assistant-shell route geometry | Feature pages with left work panel + main detail |

---

**Language**: All documentation must be written in **English**.
