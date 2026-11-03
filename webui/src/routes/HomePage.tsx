/** The home screen: every module as a tile; a tile opens the module's projects. */
import { RightOutlined } from '@ant-design/icons';
import { Card, Col, Row, Typography } from 'antd';
import { Link } from 'react-router-dom';
import { useModuleStatus } from '../api/workspace';
import { PageHeading } from '../components/bits';
import { MODULES } from '../modules';
import { MODULES_TEXT } from '../vocabulary';

export default function HomePage() {
  const status = useModuleStatus();
  const projects = status.data ?? [];
  return (
    <>
      <PageHeading title={MODULES_TEXT.title} subtitle={MODULES_TEXT.subtitle} />
      <Row gutter={[16, 16]}>
        {MODULES.map((m) => {
          const ready = projects.filter(m.ready).length;
          const href = `/modules/${m.key}`;
          return (
            <Col key={m.key} xs={24} sm={12} xl={8}>
              <Link to={href} className="module-tile-link" aria-label={m.title}>
                <Card hoverable className="module-tile">
                  <div className="module-tile-head">
                    <span className="module-tile-icon" aria-hidden>
                      <m.Icon />
                    </span>
                    <Typography.Title level={4} className="module-tile-title">
                      {m.title}
                    </Typography.Title>
                  </div>
                  <Typography.Paragraph type="secondary" className="module-tile-text">
                    {m.description}
                  </Typography.Paragraph>
                  <div className="module-tile-foot">
                    <Typography.Text type="secondary">
                      {status.isPending ? '' : MODULES_TEXT.projectsReady(ready, projects.length)}
                    </Typography.Text>
                    <span className="module-tile-open">
                      {MODULES_TEXT.open} <RightOutlined />
                    </span>
                  </div>
                </Card>
              </Link>
            </Col>
          );
        })}
      </Row>
    </>
  );
}
