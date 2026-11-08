<?php

namespace Tests\Unit;

use App\Models\User;
use PHPUnit\Framework\TestCase;

class UserTest extends TestCase
{
    public function testName(): void
    {
        $this->assertSame('a', (new User('a'))->getName());
    }

    private function helper(): void {}
}
