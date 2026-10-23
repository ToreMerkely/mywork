BIN := $(HOME)/.local/bin
APPS := $(HOME)/.local/share/applications
ICONS := $(HOME)/.local/share/icons/hicolor/scalable/apps

.PHONY: install uninstall

install:
	mkdir -p $(BIN) $(APPS) $(ICONS)
	ln -sf $(CURDIR)/mywork.py $(BIN)/mywork
	cp mywork.svg $(ICONS)/mywork.svg
	cp mywork.desktop $(APPS)/mywork.desktop
	-update-desktop-database $(APPS)

uninstall:
	rm -f $(BIN)/mywork $(ICONS)/mywork.svg $(APPS)/mywork.desktop
	-update-desktop-database $(APPS)
