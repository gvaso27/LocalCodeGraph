package com.example.app

interface ProfileRepository {
    suspend fun getProfile(id: Long): Profile
}
