// Main JavaScript functionality

document.addEventListener('DOMContentLoaded', function() {
    // Auto-hide alerts after 5 seconds
    setTimeout(function() {
        const alerts = document.querySelectorAll('.alert-dismissible');
        alerts.forEach(alert => {
            const bsAlert = new bootstrap.Alert(alert);
            bsAlert.close();
        });
    }, 5000);
    
    // Translation functionality
    window.translatePage = function() {
        const elements = document.querySelectorAll('[data-translate]');
        elements.forEach(el => {
            const text = el.getAttribute('data-translate');
            // Call translation API
            fetch('/api/translate', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({text: text, lang: 'fr'})
            })
            .then(r => r.json())
            .then(data => {
                if(data.success) {
                    el.textContent = data.translation;
                }
            });
        });
    };
});
