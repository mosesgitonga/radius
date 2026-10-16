# MyRadius app

The Flutter app for MyRadius, for Android (and iOS, which needs a Mac to build).
The backend is in the repository root; see `PROGRESS.md` for what each slice adds.

## Run it

With the backend running (`docker compose up` in the repository root):

```sh
flutter pub get
flutter run                                   # Android emulator: talks to 10.0.2.2:8000
flutter run --dart-define=API_BASE_URL=http://<your PC's Wi-Fi address>:8000   # a phone
```

Sign-in codes go nowhere in development: read them from the API's log,
`docker compose logs api | grep sms_not_sent`.

## Check it

```sh
flutter analyze
flutter test
```
