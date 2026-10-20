import { Button, Result } from 'antd';
import { Link } from 'react-router-dom';
import { APP } from '../vocabulary';

export default function NotFoundPage() {
  return (
    <Result
      status="404"
      title={APP.notFoundTitle}
      subTitle={APP.notFoundText}
      extra={
        <Link to="/">
          <Button type="primary">{APP.backToRuns}</Button>
        </Link>
      }
    />
  );
}
