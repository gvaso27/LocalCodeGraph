package com.example.app

sealed class Result {
    class Success(val value: String) : Result()
    class Failure(val error: String) : Result()
}
