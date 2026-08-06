package com.example.app

import java.util.List
import java.util.Map as JMap
import com.example.util.*

@Suppress("unused")
open class ProfileViewModel(
    private val repository: ProfileRepository,
    var retries: Int = 3
) : BaseViewModel(), Loggable, Comparable<ProfileViewModel> {

    val name: String = "profile"
    var counter: Int = 0
    private lateinit var cachedProfile: Profile

    companion object {
        const val TAG = "Profile"

        fun create(repository: ProfileRepository): ProfileViewModel =
            ProfileViewModel(repository)
    }

    override fun onCreate() {
    }

    override fun log(message: String) {
    }

    override fun compareTo(other: ProfileViewModel): Int = 0

    fun <T> identity(value: T): T = value

    inner class Session {
        fun close() {}
    }

    class Snapshot {
        fun restore() {}
    }
}
