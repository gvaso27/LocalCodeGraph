package com.example.app;

import java.util.List;
import java.util.Map;
import com.example.util.*;

import static com.example.util.Constants.MAX_SOUNDS;

@RestController
public class SoundController
        extends BaseController
        implements Loggable, Comparable<SoundController> {

    private static final int MAX = 5;
    public List<String> names;
    private Map<String, Integer> counts;

    @Autowired
    public SoundController(BaseRepository<String> repository) {
        this.repository = repository;
    }

    SoundController() {
    }

    @Override
    public void handle() {
    }

    @Override
    public void log(String message) {
    }

    @Override
    public int compareTo(SoundController other) {
        return 0;
    }

    @GetMapping("/sounds")
    public List<String> getSounds(@RequestParam String category, int limit) {
        return names;
    }

    public List<String> getSounds() {
        return names;
    }

    public <T extends Comparable<T>> T max(T a, T b) {
        return a;
    }

    public static abstract class InnerBase {
        abstract void run();
    }

    private static class InnerImpl extends InnerBase {
        void run() {
        }
    }

    protected interface InnerCallback {
        void onDone();
    }

    public enum Status implements Loggable {
        ACTIVE, INACTIVE;

        @Override
        public void log(String message) {
        }
    }

    public @interface SoundAnnotation {
        String value() default "x";

        int priority();
    }
}

class Helper {
    void assist() {
    }
}
