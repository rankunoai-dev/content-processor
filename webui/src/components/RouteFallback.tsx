import { Skeleton } from 'antd';

export function RouteFallback() {
  return (
    <div role="status" aria-label="Loading">
      <Skeleton active title paragraph={{ rows: 8 }} />
    </div>
  );
}
