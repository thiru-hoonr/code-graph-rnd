// Reference parser for "functions expected" in Java.
//
// Uses javac's own parser (the JDK compiler, com.sun.source / JavacTask.parse) — not tree-sitter,
// so it is independent of code-review-graph. Parse only: nothing is compiled, no classpath or
// project dependencies are needed.
//
// Run with the JDK's source launcher (JDK 11+), no build step:
//   java java-ref/ListMethods.java <repo root>      (relative file paths on stdin, one per line)
// Prints JSON: { "<file>": { "named": [{name, line, end, kind}], "unnamed": [{line, end}],
//                            "syntax_errors": n } }   or  { "<file>": { "error": "..." } }
//
// Named = expected (one per method declaration javac sees):
//   method               void save() {}             (also abstract / interface methods without a body)
//   constructor          Owner() {}                 (name = the class name, as code-review-graph names it)
//   compact_constructor  record P(int x) { P {} }   (records, Java 16+)
//   annotation_member    @interface A { String value(); }
// Methods inside anonymous classes count too: they have a name.
// Unnamed = not expected, reported separately: lambdas  (x -> x + 1).

import com.sun.source.tree.*;
import com.sun.source.util.*;
import javax.tools.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;

public class ListMethods {
    public static void main(String[] args) throws Exception {
        Path root = Paths.get(args[0]);
        List<String> files = new ArrayList<>();
        try (BufferedReader in = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
            for (String line; (line = in.readLine()) != null; ) if (!line.isBlank()) files.add(line.strip());
        }
        JavaCompiler javac = ToolProvider.getSystemJavaCompiler();
        // One file manager for the whole run (big repos: thousands of files); each file still gets
        // its own parse task and its own diagnostics, so one file's errors never leak into another's.
        StandardJavaFileManager fm = javac.getStandardFileManager(null, null, StandardCharsets.UTF_8);
        StringBuilder out = new StringBuilder("{");
        boolean first = true;
        for (String rel : files) {
            if (!first) out.append(',');
            first = false;
            out.append(json(rel)).append(':').append(one(javac, fm, root.resolve(rel)));
        }
        System.out.print(out.append('}'));
    }

    static String one(JavaCompiler javac, StandardJavaFileManager fm, Path file) {
        try {
            DiagnosticCollector<JavaFileObject> diags = new DiagnosticCollector<>();
            // --enable-preview / release latest so newer syntax (records, sealed, switch patterns) parses.
            JavacTask task = (JavacTask) javac.getTask(null, fm, diags,
                    List.of("-proc:none", "--release", String.valueOf(Runtime.version().feature()), "--enable-preview"),
                    null, fm.getJavaFileObjects(file.toFile()));
            SourcePositions pos = Trees.instance(task).getSourcePositions();
            StringBuilder named = new StringBuilder(), unnamed = new StringBuilder();
            for (CompilationUnitTree cu : task.parse()) {
                LineMap lines = cu.getLineMap();
                new TreePathScanner<Void, Deque<String>>() {
                    void add(StringBuilder sb, Tree t, String name, String kind) {
                        long s = pos.getStartPosition(cu, t), e = pos.getEndPosition(cu, t);
                        if (sb.length() > 0) sb.append(',');
                        sb.append('{');
                        if (name != null) sb.append("\"name\":").append(json(name)).append(',');
                        sb.append("\"line\":").append(lines.getLineNumber(s))
                          .append(",\"end\":").append(lines.getLineNumber(Math.max(s, e - 1)));
                        if (kind != null) sb.append(",\"kind\":").append(json(kind));
                        sb.append('}');
                    }
                    @Override public Void visitClass(ClassTree c, Deque<String> classes) {
                        classes.push(c.getSimpleName().toString());  // "" for an anonymous class
                        super.visitClass(c, classes);
                        classes.pop();
                        return null;
                    }
                    @Override public Void visitMethod(MethodTree m, Deque<String> classes) {
                        String name = m.getName().toString();
                        ClassTree owner = (ClassTree) getCurrentPath().getParentPath().getLeaf();
                        String kind;
                        if (name.equals("<init>")) {
                            name = classes.isEmpty() ? name : classes.peek();
                            // A compact record constructor has no parameter list: no "(" between
                            // its start and its body ("P { … }" vs "P(int x) { … }").
                            boolean compact = false;
                            if (owner.getKind() == Tree.Kind.RECORD && m.getBody() != null) {
                                try {
                                    String src = cu.getSourceFile().getCharContent(true).toString();
                                    int s = (int) pos.getStartPosition(cu, m), b = (int) pos.getStartPosition(cu, m.getBody());
                                    compact = s >= 0 && b > s && src.substring(s, b).indexOf('(') < 0;
                                } catch (IOException ignored) {
                                    // source unreadable here: leave it a plain constructor
                                }
                            }
                            kind = compact ? "compact_constructor" : "constructor";
                        } else {
                            kind = owner.getKind() == Tree.Kind.ANNOTATION_TYPE ? "annotation_member" : "method";
                        }
                        add(named, m, name, kind);
                        return super.visitMethod(m, classes);
                    }
                    @Override public Void visitLambdaExpression(LambdaExpressionTree l, Deque<String> classes) {
                        add(unnamed, l, null, null);
                        return super.visitLambdaExpression(l, classes);
                    }
                }.scan(cu, new ArrayDeque<>());
            }
            long errors = diags.getDiagnostics().stream().filter(d -> d.getKind() == Diagnostic.Kind.ERROR).count();
            return "{\"named\":[" + named + "],\"unnamed\":[" + unnamed + "],\"syntax_errors\":" + errors + "}";
        } catch (Exception ex) {
            return "{\"error\":" + json(String.valueOf(ex)) + "}";
        }
    }

    static String json(String s) {
        StringBuilder b = new StringBuilder("\"");
        for (char ch : s.toCharArray()) {
            switch (ch) {
                case '"' -> b.append("\\\"");
                case '\\' -> b.append("\\\\");
                case '\n' -> b.append("\\n");
                case '\r' -> b.append("\\r");
                case '\t' -> b.append("\\t");
                default -> { if (ch < 0x20) b.append(String.format("\\u%04x", (int) ch)); else b.append(ch); }
            }
        }
        return b.append('"').toString();
    }
}
