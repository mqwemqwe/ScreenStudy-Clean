"""Offline, clearly labelled interface demonstration. Never used for live answers."""

QUESTIONS = [{
    "question": "Какой метод добавляет элемент в конец списка Python?",
    "kind": "single", "answer": "append()",
    "question_explanation": "Нужно выбрать метод списка, который добавляет один новый элемент в его конец.",
    "explanation": "Метод append(x) добавляет объект x в конец списка. Например: a = [1, 2]; a.append(3). После этого a равен [1, 2, 3]. Метод extend() добавляет элементы из другого итерируемого объекта.",
    "confidence": "high", "line_ids": [0],
    "options": [{"text": "append()", "correct": True, "line_ids": [1]},
                {"text": "remove()", "correct": False, "line_ids": [2]},
                {"text": "sort()", "correct": False, "line_ids": [3]}]
}, {
    "question": "Объясните, что такое ранг матрицы.",
    "kind": "text", "answer": "Ранг матрицы — максимальное число её линейно независимых строк или столбцов.",
    "question_explanation": "Нужно дать определение ранга матрицы.",
    "explanation": "После приведения матрицы к ступенчатому виду её ранг равен числу ненулевых строк. Например, строки (1, 2) и (2, 4) зависимы: вторая вдвое больше первой, поэтому ранг этой матрицы равен 1.",
    "confidence": "high", "line_ids": [], "options": []
}]


def populate(window):
    window.show_questions(QUESTIONS)
    window.status.setText("ДЕМО · пример интерфейса, экран не захватывается")
