<?php

namespace App\Support;

final class Str
{
    public static function lower(string $s): string
    {
        return strtolower($s);
    }
}
