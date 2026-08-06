package com.example.app

@Composable
fun ProfileScreen(
    userId: Long,
    onNavigate: () -> Unit,
    label: String = "default",
    vararg tags: String
) {
}

infix fun Int.times2(x: Int): Int = this * x

fun String.toFoo(): Int = this.length

val String.lastChar: Char
    get() = this[length - 1]

val topLevelVal: Int = 5
var topLevelVar = "hello"
