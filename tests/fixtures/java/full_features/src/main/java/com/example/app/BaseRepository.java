package com.example.app;

public interface BaseRepository<T> {
    T findById(long id);
}
