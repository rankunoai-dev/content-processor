import { Button, Result } from 'antd';
import { Component, type ErrorInfo, type ReactNode } from 'react';
import { APP } from '../vocabulary';

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/** Keeps one broken screen from taking down the whole app. Wrapped around every route. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('Screen error', error, info.componentStack);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <Result
          status="error"
          title={APP.crashTitle}
          subTitle={APP.crashText}
          extra={
            <Button type="primary" onClick={() => window.location.reload()}>
              {APP.reload}
            </Button>
          }
        />
      );
    }
    return this.props.children;
  }
}
