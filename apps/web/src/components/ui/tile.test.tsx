import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../api/client';
import { Donut, Meter, Metric, Sparkline, Tile, donutArcs, meterPercent, sparklineSegments } from '.';

describe('Tile', () => {
  it('section с заголовком, подпись источника; загрузка — aria-busy и скелет', () => {
    const { rerender } = render(
      <Tile id="t1" title="Проекты" icon="folder" source="Источник: listProjects">
        <p>42</p>
      </Tile>,
    );
    const region = screen.getByRole('region', { name: 'Проекты' });
    expect(region).toHaveAttribute('data-state', 'ready');
    expect(within(region).getByText('Источник: listProjects')).toBeInTheDocument();
    rerender(<Tile id="t1" title="Проекты" loading source="x" />);
    expect(screen.getByRole('region', { name: 'Проекты' })).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByTestId('tile-skeleton')).toBeInTheDocument();
    expect(screen.queryByText('x')).toBeNull();
  });

  it('ошибка — текст problem+json, request id и «Повторить»', async () => {
    const onRetry = vi.fn();
    const error = new ApiError({ status: 500, code: 'internal_error', requestId: 'req-9' });
    render(<Tile id="t2" title="Находки" error={error} onRetry={onRetry} />);
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Не удалось загрузить данные');
    expect(alert).toHaveTextContent('req-9');
    await userEvent.click(within(alert).getByRole('button', { name: 'Повторить' }));
    expect(onRetry).toHaveBeenCalledOnce();
  });
});

describe('Metric', () => {
  it('null — «нет данных», а не 0; more — «N+»', () => {
    const { rerender } = render(<Metric label="Доля" value={null} />);
    expect(screen.getByText('нет данных')).toBeInTheDocument();
    rerender(<Metric label="Доля" value={null} empty="нет завершённых запусков" />);
    expect(screen.getByText('нет завершённых запусков')).toBeInTheDocument();
    rerender(<Metric label="Задачи" value={50} more />);
    expect(screen.getByText('50+')).toBeInTheDocument();
    rerender(<Metric label="Задачи" value={0} />);
    expect(screen.getByText('0')).toBeInTheDocument();
  });
});

describe('charts', () => {
  it('sparklineSegments: null разрывает линию, одна точка — по центру', () => {
    const segs = sparklineSegments([1, 2, null, 4], 100, 20, 0);
    expect(segs).toHaveLength(2);
    expect(segs[0]).toEqual([
      [0, 15],
      [33.33, 10],
    ]);
    expect(segs[1]).toEqual([[100, 0]]);
    expect(sparklineSegments([null, null], 100, 20)).toEqual([]);
    expect(sparklineSegments([5], 100, 20)[0]?.[0]?.[0]).toBe(50);
  });

  it('Sparkline: role=img с текстовой альтернативой и таблица значений', () => {
    render(
      <Sparkline
        summary="Завершено по дням: всего 3"
        points={[
          { label: '1 нояб.', value: 1 },
          { label: '2 нояб.', value: null },
          { label: '3 нояб.', value: 2 },
        ]}
      />,
    );
    expect(screen.getByRole('img', { name: 'Завершено по дням: всего 3' })).toBeInTheDocument();
    const table = screen.getByRole('table', { name: 'Завершено по дням: всего 3' });
    expect(within(table).getAllByRole('row')).toHaveLength(4);
    expect(within(table).getByText('нет данных')).toBeInTheDocument();
  });

  it('Meter: полоса декоративна, значение — текстом; null — пустая полоса', () => {
    expect(meterPercent(null)).toBe(0);
    expect(meterPercent(1.5)).toBe(100);
    expect(meterPercent(0.25)).toBe(25);
    render(<Meter label="Успешно" ratio={null} valueText="нет данных" testId="m" />);
    expect(screen.getByTestId('m')).toHaveAttribute('data-ratio', 'none');
    expect(screen.getByText('нет данных')).toBeInTheDocument();
  });

  it('donutArcs: доли окружности; все нули — пустое кольцо', () => {
    expect(donutArcs([1, 3], 100)).toEqual([
      { length: 25, offset: -0 },
      { length: 75, offset: -25 },
    ]);
    expect(donutArcs([0, 0], 100).every((a) => a.length === 0)).toBe(true);
  });

  it('Donut: сводка в aria-label, легенда со значениями текстом', () => {
    render(
      <Donut
        summary="Исходы: FAIL — 2"
        legendLabel="Исходы"
        segments={[
          { key: 'FAIL', label: 'Не пройдено', value: 2, strokeClass: 'stroke-danger', swatchClass: 'bg-danger' },
          { key: 'NOT_RUN', label: 'Не запускалось', value: 0, strokeClass: 'stroke-neutral', swatchClass: 'bg-neutral' },
        ]}
      />,
    );
    expect(screen.getByRole('img', { name: 'Исходы: FAIL — 2' })).toBeInTheDocument();
    const legend = screen.getByRole('list', { name: 'Исходы' });
    expect(within(legend).getAllByRole('listitem')).toHaveLength(2);
    expect(legend).toHaveTextContent('Не пройдено2');
  });
});
