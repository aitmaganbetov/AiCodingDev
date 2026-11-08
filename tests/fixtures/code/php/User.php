<?php

declare(strict_types=1);

namespace App\Models;

use App\Contracts\HasName;
use App\Support\{Str, Arr as A};
use function App\Support\helper;

require_once __DIR__ . '/bootstrap.php';
include 'config/app.php';

const VERSION = '1.0';

function format_name(string $name): string
{
    return trim($name);
}

/**
 * Модель пользователя.
 * @property string $name
 */
final class User extends Model implements HasName
{
    public const MAX_NAME = 255;
    private const SECRET = 'x';

    public function __construct(private string $name)
    {
    }

    /** Имя пользователя. */
    public function getName(): string
    {
        return $this->name;
    }

    public static function make(string $name): self
    {
        return new self($name);
    }
}

interface Repository
{
    public function find(int $id): ?User;
}

trait Timestamps
{
    public function touch(): void {}
}

enum Status: string
{
    case Active = 'active';
}
