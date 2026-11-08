<?php

namespace App\Models;

use App\Support\Str;
use Illuminate\Database\Eloquent\Model;

class User extends Model
{
    public function name(): string
    {
        return Str::lower('X');
    }
}
