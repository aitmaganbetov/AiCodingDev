<?php

namespace Tests;

use App\Models\User;
use PHPUnit\Framework\TestCase;

final class UserTest extends TestCase
{
    public function testName(): void
    {
        $this->assertSame('x', (new User())->name());
    }
}
