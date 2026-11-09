import js from '@eslint/js';
import jsxA11y from 'eslint-plugin-jsx-a11y';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist', 'dist-e2e-*', 'dist-live', 'node_modules', 'test-results', 'playwright-report', 'src/api/schema.d.ts'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: { globals: globals.browser },
    plugins: { 'react-hooks': reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
    },
  },
  {
    files: ['src/**/*.tsx'],
    ...jsxA11y.flatConfigs.strict,
  },
  // Иконки — только через components/ui/Icon (карта имён, tree-shaking именованных импортов Lucide).
  {
    files: ['src/**/*.{ts,tsx}'],
    ignores: ['src/components/ui/Icon.tsx'],
    rules: {
      'no-restricted-imports': ['error', { paths: [{ name: 'lucide-react', message: 'Используйте <Icon name="…"> из components/ui (карта имён в Icon.tsx).' }] }],
    },
  },
  {
    files: ['src/components/ui/Icon.tsx'],
    rules: {
      'no-restricted-syntax': [
        'error',
        { selector: "ImportDeclaration[source.value='lucide-react'] > ImportNamespaceSpecifier", message: 'Только именованные импорты Lucide (tree-shaking).' },
      ],
    },
  },
  {
    files: ['*.{js,ts,mjs}', 'scripts/**/*.mjs', 'e2e/**/*.ts'],
    languageOptions: { globals: globals.node },
  },
);
