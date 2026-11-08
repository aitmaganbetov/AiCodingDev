import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';
import { authApi } from '../api/auth';
import { clearSession, useSession } from '../auth/session';
import { Icon } from '../components/ui/Icon';
import { Menu, MenuContent, MenuItem, MenuLabel, MenuRadioGroup, MenuRadioItem, MenuSeparator, MenuTrigger } from '../components/ui/DropdownMenu';
import { THEMES, useTheme, type ThemePreference } from '../lib/theme';

/** Меню пользователя (Radix DropdownMenu): тема оформления и выход. */
export function UserMenu() {
  const { t } = useTranslation();
  const { data: session } = useSession();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [theme, setTheme] = useTheme();
  const logout = useMutation({
    mutationFn: () => authApi.logout(),
    // Локальную сессию очищаем в любом случае: даже если сервер ответил 401/сеть недоступна.
    onSettled: () => {
      clearSession(queryClient, { explicitLogout: true });
      navigate('/login', { replace: true });
    },
  });

  if (!session) return null;
  return (
    <Menu>
      <MenuTrigger className="btn-secondary">
        <Icon name="user" className="h-4 w-4 shrink-0" />
        <span className="max-w-[7rem] truncate sm:max-w-[14rem]">
          <span className="sr-only">{t('layout.userMenu')}: </span>
          {session.user.display_name}
        </span>
        <Icon name="chevronDown" className="h-4 w-4 shrink-0" />
      </MenuTrigger>
      <MenuContent>
        <MenuLabel>
          <span className="break-all font-normal">{session.user.email}</span>
        </MenuLabel>
        <MenuSeparator />
        <MenuLabel>{t('theme.label')}</MenuLabel>
        <MenuRadioGroup value={theme} onValueChange={(v) => setTheme(v as ThemePreference)}>
          {THEMES.map((th) => (
            <MenuRadioItem key={th} value={th}>
              {t(`theme.${th}`)}
            </MenuRadioItem>
          ))}
        </MenuRadioGroup>
        <MenuSeparator />
        <MenuItem disabled={logout.isPending} onSelect={() => logout.mutate()}>
          {logout.isPending ? t('auth.loggingOut') : t('auth.logout')}
        </MenuItem>
      </MenuContent>
    </Menu>
  );
}
