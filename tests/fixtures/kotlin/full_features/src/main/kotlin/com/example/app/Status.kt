package com.example.app

enum class Status(val code: Int) : Loggable {
    ACTIVE(1),
    INACTIVE(0);

    override fun log(message: String) {
    }
}
