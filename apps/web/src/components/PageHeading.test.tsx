import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { MemoryRouter, useNavigate } from 'react-router';
import { describe, expect, it } from 'vitest';
import { PageHeading, useScreenChangeTracker } from './PageHeading';

/** Модель оболочки: поиск в верхней панели (вне main) и экран с заголовком, который перемонтируется. */
function Shell({ loaded, withField = false }: { loaded: boolean; withField?: boolean }) {
  useScreenChangeTracker();
  const navigate = useNavigate();
  return (
    <>
      <header>
        <input aria-label="Поиск" />
        <button type="button" onClick={() => navigate('/b')}>
          На экран B
        </button>
      </header>
      <main>
        {withField ? <input aria-label="Фильтр" /> : null}
        {/* Загрузка → данные: заголовок в разных ветках — новый экземпляр (как в экранах с LoadingState). */}
        {loaded ? <PageHeading key="loaded" title="Данные" /> : <PageHeading key="loading" title="Загрузка" />}
      </main>
    </>
  );
}

function Harness({ withField = false }: { withField?: boolean }) {
  const [loaded, setLoaded] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setLoaded(true)}>
        загрузить
      </button>
      <Shell loaded={loaded} withField={withField} />
    </>
  );
}

describe('PageHeading focus (WCAG 2.4.3, no focus theft)', () => {
  it('moves focus to h1 once per screen change, not again when the heading remounts while the user types', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/a']}>
        <Harness />
      </MemoryRouter>,
    );
    expect(screen.getByRole('heading', { level: 1, name: 'Загрузка' })).toHaveFocus();
    // Пользователь начинает печатать в поиске, пока данные экрана грузятся.
    await user.click(screen.getByLabelText('Поиск'));
    await user.keyboard('отч');
    act(() => screen.getByRole('button', { name: 'загрузить' }).click());
    await user.click(screen.getByLabelText('Поиск'));
    await user.keyboard('ёт');
    expect(screen.getByRole('heading', { level: 1, name: 'Данные' })).toBeInTheDocument();
    expect(screen.getByLabelText('Поиск')).toHaveFocus();
    expect(screen.getByLabelText('Поиск')).toHaveValue('отчёт');
  });

  it('a new screen (pathname) moves focus again; a lost focus (focused element removed) is restored to h1', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/a']}>
        <Harness />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole('button', { name: 'На экран B' }));
    expect(screen.getByRole('heading', { level: 1, name: 'Загрузка' })).toHaveFocus();
    // Заголовок с фокусом удаляется при перемонтировании — фокус не остаётся на body.
    act(() => screen.getByRole('button', { name: 'загрузить' }).click());
    expect(screen.getByRole('heading', { level: 1, name: 'Данные' })).toHaveFocus();
  });

  it('does not take focus from a field in main the user already typed into', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/a']}>
        <Harness withField />
      </MemoryRouter>,
    );
    await user.click(screen.getByLabelText('Фильтр'));
    await user.keyboard('abc');
    await user.click(screen.getByRole('button', { name: 'На экран B' }));
    // Новый экран: фокус на h1 (кнопка перехода — вне main).
    expect(screen.getByRole('heading', { level: 1, name: 'Загрузка' })).toHaveFocus();
    await user.click(screen.getByLabelText('Фильтр'));
    act(() => screen.getByRole('button', { name: 'загрузить' }).click());
    expect(screen.getByLabelText('Фильтр')).toHaveFocus();
  });
});
